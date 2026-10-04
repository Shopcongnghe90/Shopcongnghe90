"""Evaluation Engine (Workstream B)."""

from zeus.evals.graders import grade, validate_schema
from zeus.evals.runner import (
    CaseResult,
    EvalCase,
    EvalReport,
    PgEvalStore,
    add_regression_case,
    load_cases,
    oracle_candidate,
    run_eval,
    write_report,
)

__all__ = ["grade", "validate_schema", "CaseResult", "EvalCase", "EvalReport", "PgEvalStore", "add_regression_case",
           "load_cases", "oracle_candidate", "run_eval", "write_report"]
