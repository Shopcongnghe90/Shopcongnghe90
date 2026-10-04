"""Temporal activities của B (task queue ``TASK_QUEUE_BRAIN``). Worker (integrator) đăng ký ``BrainActivities(...).all()``."""

from __future__ import annotations

from collections.abc import Callable

from temporalio import activity

from zeus.contracts.interfaces import BrainRetriever, EvidenceStore, OutcomeRecorder
from zeus.contracts.models import ContextPacket, DatasetRecord, EvidenceRecord, RetrievalHit, RetrievalQuery, Task


class BrainActivities:
    def __init__(self, retriever: BrainRetriever, evidence: EvidenceStore, recorder: OutcomeRecorder) -> None:
        self._r, self._e, self._o = retriever, evidence, recorder

    @activity.defn(name="zeus.brain.retrieve")
    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]:
        return await self._r.retrieve(query)

    @activity.defn(name="zeus.brain.build_context")
    async def build_context(self, query: RetrievalQuery, task: Task | None = None) -> ContextPacket:
        return await self._r.build_context(query, task)

    @activity.defn(name="zeus.evidence.put")
    async def put_evidence(self, record: EvidenceRecord) -> str:
        return await self._e.put(record)

    @activity.defn(name="zeus.learning.record_outcome")
    async def record_outcome(self, evidence: EvidenceRecord) -> DatasetRecord:
        return await self._o.record(evidence)

    def all(self) -> list[Callable[..., object]]:
        return [self.retrieve, self.build_context, self.put_evidence, self.record_outcome]
