"""Dataset pipeline RAW -> NORMALIZED -> VERIFIED -> CURATED có cổng.

Cổng: không bỏ cấp; VERIFIED chỉ khi EvidenceRecord (cùng tenant) có outcome verified + bằng chứng mạnh — outcome lấy từ
evidence, KHÔNG từ người gọi; CURATED chỉ từ VERIFIED_SUCCESS và bắt buộc đã che PII. Xuất train chỉ lấy CURATED.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from psycopg.types.json import Jsonb

from zeus.brain.pg import PgBase
from zeus.contracts.models import DatasetRecord, DatasetStage, Outcome, TaskFamily
from zeus.evidence.rules import check_record
from zeus.evidence.store import PgEvidenceStore

_PHONE = re.compile(r"(?<!\d)(?:\+?84|0)[\s.-]?\d{2,3}[\s.-]?\d{3}[\s.-]?\d{3,4}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact_pii(text: str) -> str:
    return _EMAIL.sub("[EMAIL]", _PHONE.sub("[PHONE]", text))


def _redact_obj(o: Any) -> Any:
    if isinstance(o, str):
        return redact_pii(o)
    if isinstance(o, dict):
        return {k: _redact_obj(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact_obj(v) for v in o]
    return o


class PromotionError(RuntimeError):
    pass


_ORDER = [DatasetStage.RAW, DatasetStage.NORMALIZED, DatasetStage.VERIFIED, DatasetStage.CURATED]


def derived_id(parent: str, stage: DatasetStage) -> str:
    return "dsr_" + hashlib.sha256(f"{parent}:{stage.value}".encode()).hexdigest()[:32]


def _row(r: dict[str, Any]) -> DatasetRecord:
    return DatasetRecord(
        record_id=r["record_id"], tenant_id=r["tenant_id"], stage=DatasetStage(r["stage"]),
        task_family=TaskFamily(r["task_family"]), input=r["input"], output=r["output"],
        evidence_record_id=r["evidence_record_id"], outcome=Outcome(r["outcome"]), labels=r["labels"],
        pii_redacted=r["pii_redacted"], source=r["source"], promoted_from=r["promoted_from"], created_at=r["created_at"],
    )


class DatasetPipeline(PgBase):
    def __init__(self, dsn: str, evidence: PgEvidenceStore | None = None) -> None:
        super().__init__(dsn)
        self.evidence = evidence or PgEvidenceStore(dsn)

    async def insert(self, rec: DatasetRecord) -> DatasetRecord:
        """Ghi 1 record (idempotent theo record_id). Validator contract đã chặn VERIFIED/CURATED thiếu evidence."""
        async with self.conn() as c:
            await c.execute(
                "INSERT INTO dataset_records (record_id, tenant_id, stage, task_family, input, output, evidence_record_id, "
                "outcome, labels, pii_redacted, source, promoted_from, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (record_id) DO NOTHING",
                (rec.record_id, rec.tenant_id, rec.stage.value, rec.task_family.value, Jsonb(rec.input), Jsonb(rec.output),
                 rec.evidence_record_id, rec.outcome.value, Jsonb(rec.labels), rec.pii_redacted, rec.source,
                 rec.promoted_from, rec.created_at),
            )
        return rec

    async def get(self, tenant_id: str, record_id: str) -> DatasetRecord | None:
        async with self.conn() as c:
            r = await (await c.execute(
                "SELECT * FROM dataset_records WHERE tenant_id=%s AND record_id=%s", (tenant_id, record_id))).fetchone()
        return _row(r) if r else None

    async def list(self, tenant_id: str, stage: DatasetStage | None = None, family: TaskFamily | None = None) -> list[DatasetRecord]:
        sql, args = "SELECT * FROM dataset_records WHERE tenant_id=%s", [tenant_id]
        if stage:
            sql += " AND stage=%s"
            args.append(stage.value)
        if family:
            sql += " AND task_family=%s"
            args.append(family.value)
        async with self.conn() as c:
            return [_row(r) for r in await (await c.execute(sql + " ORDER BY created_at, record_id", args)).fetchall()]

    async def _load(self, tenant_id: str, record_id: str, expect: DatasetStage) -> DatasetRecord:
        rec = await self.get(tenant_id, record_id)
        if rec is None:
            raise PromotionError(f"không thấy dataset record {record_id} trong tenant {tenant_id}")
        if rec.stage is not expect:
            raise PromotionError(f"cần stage {expect.value}, record đang {rec.stage.value} (không được nhảy cấp)")
        return rec

    async def normalize(self, tenant_id: str, record_id: str) -> DatasetRecord:
        rec = await self._load(tenant_id, record_id, DatasetStage.RAW)
        inp = {k: (" ".join(v.split()) if isinstance(v, str) else v) for k, v in rec.input.items()}
        out = {k: (" ".join(v.split()) if isinstance(v, str) else v) for k, v in rec.output.items()}
        if not inp or not out:
            raise PromotionError("RAW thiếu input/output, không thể NORMALIZED")
        new = rec.model_copy(update={"record_id": derived_id(rec.record_id, DatasetStage.NORMALIZED),
                                     "stage": DatasetStage.NORMALIZED, "input": inp, "output": out,
                                     "promoted_from": rec.record_id})
        return await self.insert(new)

    async def verify(self, tenant_id: str, record_id: str, evidence_record_id: str | None = None) -> DatasetRecord:
        rec = await self._load(tenant_id, record_id, DatasetStage.NORMALIZED)
        ev_id = evidence_record_id or rec.evidence_record_id
        if not ev_id:
            raise PromotionError("thiếu evidence_record_id: không thể VERIFIED")
        ev = await self.evidence.get(ev_id, tenant_id)
        if ev is None:
            raise PromotionError("evidence không tồn tại trong tenant này")
        if ev.final_outcome is Outcome.UNVERIFIED:
            raise PromotionError("evidence chưa verified: không promote dữ liệu chưa verified")
        try:
            check_record(ev)
        except ValueError as exc:
            raise PromotionError(str(exc)) from exc
        new = rec.model_copy(update={"record_id": derived_id(rec.record_id, DatasetStage.VERIFIED),
                                     "stage": DatasetStage.VERIFIED, "evidence_record_id": ev_id,
                                     "outcome": ev.final_outcome, "promoted_from": rec.record_id})
        return await self.insert(new)

    async def curate(self, tenant_id: str, record_id: str, redactor: Callable[[Any], Any] = _redact_obj) -> DatasetRecord:
        rec = await self._load(tenant_id, record_id, DatasetStage.VERIFIED)
        if rec.outcome is not Outcome.VERIFIED_SUCCESS:
            raise PromotionError("chỉ VERIFIED_SUCCESS được CURATED làm dữ liệu huấn luyện")
        new = rec.model_copy(update={"record_id": derived_id(rec.record_id, DatasetStage.CURATED),
                                     "stage": DatasetStage.CURATED, "input": redactor(rec.input),
                                     "output": redactor(rec.output), "pii_redacted": True, "promoted_from": rec.record_id})
        return await self.insert(new)

    async def export_training(self, tenant_id: str, family: TaskFamily | None = None) -> list[dict[str, Any]]:
        """Chỉ CURATED + VERIFIED_SUCCESS + có evidence + PII đã che. Không có đường nào khác ra khỏi pipeline."""
        out = []
        for r in await self.list(tenant_id, DatasetStage.CURATED, family):
            if r.outcome is Outcome.VERIFIED_SUCCESS and r.evidence_record_id and r.pii_redacted:
                out.append({"record_id": r.record_id, "family": r.task_family.value, "input": r.input, "output": r.output})
        return out
