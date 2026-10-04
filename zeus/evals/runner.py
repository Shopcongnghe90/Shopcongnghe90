"""Eval runner: nạp dataset versioned, chạy candidate (hàm async), chấm bằng grader, xuất report JSON + lưu PG."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from zeus.brain.pg import PgBase
from zeus.contracts.models import EVAL_TASK_FAMILIES, EvidenceRecord, Outcome, TaskFamily
from zeus.evals.graders import grade

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "evals" / "datasets"


class EvalCase(BaseModel):
    case_id: str
    family: TaskFamily
    version: str = "v1"
    source: str = "seed"  # seed | regression
    input: dict[str, Any]
    expected: dict[str, Any] = Field(default_factory=dict)
    grader: dict[str, Any]
    tags: list[str] = Field(default_factory=list)


class CaseResult(BaseModel):
    case_id: str
    family: str
    source: str
    passed: bool
    detail: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0


class EvalReport(BaseModel):
    run_id: str = Field(default_factory=lambda: f"evr_{uuid.uuid4().hex}")
    candidate: str
    n_cases: int
    n_passed: int
    pass_rate: float
    regression_failures: int
    cost_usd: float
    avg_latency_ms: float
    by_family: dict[str, dict[str, Any]]
    results: list[CaseResult]


Candidate = Callable[[EvalCase], Awaitable["str | tuple[str, float]"]]
"""Candidate nhận case, trả output (str) hoặc (output, cost_usd)."""


def load_cases(root: Path | str = DEFAULT_ROOT, families: list[TaskFamily] | None = None, version: str = "v1") -> list[EvalCase]:
    """Nạp ``<root>/<family>/<version>.jsonl`` + ``regression.jsonl`` (nếu có)."""
    root = Path(root)
    cases: list[EvalCase] = []
    for fam in families or list(EVAL_TASK_FAMILIES):
        for name in (version, "regression"):
            p = root / fam.value / f"{name}.jsonl"
            if p.exists():
                cases += [EvalCase.model_validate_json(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    ids = [c.case_id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case_id trùng trong dataset")
    return cases


async def oracle_candidate(case: EvalCase) -> str:
    """Candidate giả: trả đúng ``expected.output`` (kiểm tra bản thân dataset/grader nhất quán)."""
    return str(case.expected["output"])


async def run_eval(cases: list[EvalCase], candidate: Candidate, name: str = "candidate") -> EvalReport:
    results: list[CaseResult] = []
    for c in cases:
        t0 = time.perf_counter()
        cost = 0.0
        try:
            res = await candidate(c)
            out, cost = (res, 0.0) if isinstance(res, str) else (res[0], float(res[1]))
            ok, detail = grade(out, c.grader)
        except Exception as exc:  # candidate lỗi = case fail, không làm hỏng cả run
            ok, detail = False, f"lỗi candidate: {type(exc).__name__}"
        results.append(CaseResult(case_id=c.case_id, family=c.family.value, source=c.source, passed=ok, detail=detail,
                                  latency_ms=int((time.perf_counter() - t0) * 1000), cost_usd=cost))
    by_family: dict[str, dict[str, Any]] = {}
    for r in results:
        f = by_family.setdefault(r.family, {"n": 0, "passed": 0})
        f["n"] += 1
        f["passed"] += int(r.passed)
    for f in by_family.values():
        f["pass_rate"] = f["passed"] / f["n"]
    n = len(results)
    passed = sum(r.passed for r in results)
    return EvalReport(
        candidate=name, n_cases=n, n_passed=passed, pass_rate=passed / n if n else 0.0,
        regression_failures=sum(1 for r in results if r.source == "regression" and not r.passed),
        cost_usd=sum(r.cost_usd for r in results), avg_latency_ms=(sum(r.latency_ms for r in results) / n) if n else 0.0,
        by_family=by_family, results=results,
    )


def write_report(report: EvalReport, path: Path | str) -> None:
    Path(path).write_text(report.model_dump_json(indent=2), encoding="utf-8")


def add_regression_case(evidence: EvidenceRecord, expected_output: str, grader: dict[str, Any],
                        root: Path | str = DEFAULT_ROOT) -> EvalCase | None:
    """Biến một failure production ĐÃ VERIFIED thành regression case. Idempotent theo evidence.record_id.
    Chỉ nhận VERIFIED_FAILURE (claims are not evidence); family phải thuộc 18 family eval."""
    if evidence.final_outcome is not Outcome.VERIFIED_FAILURE:
        raise ValueError("chỉ VERIFIED_FAILURE mới thành regression case")
    if evidence.task_family not in EVAL_TASK_FAMILIES:
        raise ValueError("family không thuộc corpus eval")
    cid = f"{evidence.task_family.value}-reg-{hashlib.sha256(evidence.record_id.encode()).hexdigest()[:10]}"
    d = Path(root) / evidence.task_family.value
    d.mkdir(parents=True, exist_ok=True)
    p = d / "regression.jsonl"
    if p.exists() and any(json.loads(ln)["case_id"] == cid for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()):
        return None
    case = EvalCase(case_id=cid, family=evidence.task_family, version="regression", source="regression",
                    input={"prompt": evidence.goal}, expected={"output": expected_output}, grader=grader,
                    tags=[f"evidence:{evidence.record_id}"])
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n")
    return case


class PgEvalStore(PgBase):
    async def register_datasets(self, tenant_id: str, root: Path | str = DEFAULT_ROOT, version: str = "v1") -> int:
        n = 0
        for fam in EVAL_TASK_FAMILIES:
            p = Path(root) / fam.value / f"{version}.jsonl"
            if not p.exists():
                continue
            data = p.read_bytes()
            async with self.conn() as c:
                await c.execute(
                    "INSERT INTO eval_datasets (tenant_id, family, version, sha256, n_cases) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (tenant_id, fam.value, version, hashlib.sha256(data).hexdigest(), len(data.splitlines())))
            n += 1
        return n

    async def save_report(self, tenant_id: str, report: EvalReport) -> str:
        async with self.conn(autocommit=False) as c:
            await c.execute(
                "INSERT INTO eval_runs (run_id, tenant_id, candidate, n_cases, n_passed, cost_usd, report) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (report.run_id, tenant_id, report.candidate, report.n_cases, report.n_passed, report.cost_usd,
                 Jsonb(report.model_dump(mode="json"))))
            for r in report.results:
                await c.execute(
                    "INSERT INTO eval_results (run_id, case_id, tenant_id, family, passed, detail, latency_ms) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (report.run_id, r.case_id, tenant_id, r.family, r.passed, r.detail, r.latency_ms))
            await c.commit()
        return report.run_id
