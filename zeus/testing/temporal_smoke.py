"""Smoke workflow Temporal: chứng minh contract pydantic đi qua Temporal (pydantic_data_converter),
activity tạo EvidenceRecord có bằng chứng thực thi, judge deterministic trả Verdict.

Không phải workflow nghiệp vụ (workflow thật thuộc A: zeus/orchestration).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    import subprocess
    import sys

    from zeus.contracts.models import (
        EvidenceItem,
        EvidenceKind,
        EvidenceRecord,
        Outcome,
        Task,
        TestRun,
        Verdict,
        VerdictDecision,
    )


@activity.defn(name="zeus.smoke.execute")
async def smoke_execute(task: Task) -> EvidenceRecord:
    """Thực thi thật một lệnh cục bộ (python -c) và lấy exit code làm bằng chứng."""
    proc = subprocess.run([sys.executable, "-c", "print(6*7)"], capture_output=True, text=True, timeout=30)
    ok = proc.returncode == 0 and proc.stdout.strip() == "42"
    info = activity.info()
    return EvidenceRecord(
        trace_id=(task.trace.trace_id if task.trace else "0" * 31 + "1"),
        tenant_id=task.tenant_id,
        workflow_id=info.workflow_id,
        task_id=task.task_id,
        task_family=task.family,
        goal=task.goal,
        tools=["local.python"],
        worker_id="control-smoke",
        tests=[TestRun(name="smoke", command="python -c print(6*7)", passed=1 if ok else 0, failed=0 if ok else 1, exit_code=proc.returncode)],
        evidence=[EvidenceItem(kind=EvidenceKind.TEST_RESULT, summary=f"stdout={proc.stdout.strip()}", passed=ok)],
        final_outcome=Outcome.VERIFIED_SUCCESS if ok else Outcome.VERIFIED_FAILURE,
        verified_by="exit_code",
    )


@activity.defn(name="zeus.smoke.judge")
async def smoke_judge(record: EvidenceRecord) -> Verdict:
    if record.final_outcome is Outcome.VERIFIED_SUCCESS:
        return Verdict(task_id=record.task_id, decision=VerdictDecision.PASS, outcome=Outcome.VERIFIED_SUCCESS, judge="deterministic", evidence_ids=[record.record_id])
    return Verdict(task_id=record.task_id, decision=VerdictDecision.FAIL, outcome=Outcome.VERIFIED_FAILURE, judge="deterministic", evidence_ids=[record.record_id])


@workflow.defn(name="ZeusSmokeWorkflow")
class SmokeWorkflow:
    @workflow.run
    async def run(self, task: Task) -> Verdict:
        record = await workflow.execute_activity(smoke_execute, task, start_to_close_timeout=timedelta(seconds=60))
        return await workflow.execute_activity(smoke_judge, record, start_to_close_timeout=timedelta(seconds=30))
