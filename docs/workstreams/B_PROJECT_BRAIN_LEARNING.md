# Workstream B — PROJECT BRAIN & LEARNING

## OBJECTIVE
Xây trí nhớ dự án và vòng học: canonical state, decision ledger store, episodic/semantic/artifact/evidence memory, hybrid
retrieval (FTS + pgvector + rerank) với context budgeter/conflict resolver/compactor/retention/backup; Evidence Engine;
Learning Plane (RAW→NORMALIZED→VERIFIED→CURATED, outcome tracking, router stats, champion/challenger); Evaluation Engine
với regression corpus 18 family.

## OWNED PATHS
- `zeus/brain/**`, `zeus/evidence/**`, `zeus/learning/**`, `zeus/evals/**`
- `evals/**` (gồm `evals/datasets/<family>/*.jsonl`)
- `config/brain.yaml`
- `migrations/2xx_*.sql` (bắt đầu 201)
- `tests/brain_learning/**` (có `__init__.py`), `docs/reports/B/**`

## DO NOT TOUCH
Shared paths (xem kiến trúc mục 11) và owned paths A/C/D. Đổi contract ⇒ CONTRACT_CHANGE_REQUEST.

## INTERFACES
- **Triển khai:** `MemoryStore`, `BrainRetriever`, `EmbeddingProvider` (client `/v1/embeddings` OpenAI-compatible tới local GPU,
  fallback: tắt vector ⇒ lexical-only), `Reranker`, `EvidenceStore`, `OutcomeRecorder`.
- **Kiểu:** `MemoryItem`, `RetrievalQuery`, `RetrievalHit`, `ContextPacket`, `HandoffPacket`, `EvidenceRecord`, `EvidenceItem`,
  `TestRun`, `ArtifactRef`, `DatasetRecord`, `RouterStat`, `Outcome`, `DatasetStage`, `TrustLevel`.
- **Temporal:** activities trên `TASK_QUEUE_BRAIN` (retrieve, build_context, put_evidence, record_outcome) — tên activity
  `zeus.brain.*`, `zeus.evidence.*`, `zeus.learning.*`.
- **Tiêu thụ:** `ModelBroker` (A) cho compactor/summary — qua Protocol, test bằng fake.

## DEPENDENCIES
PG16 + pgvector (fixture `pg_dsn`, extension đã tạo), `000_core.sql` (`tenants`). Không cần GPU (embedding fake/lexical trong test).

## ACCEPTANCE
1. Mọi bảng có `tenant_id` FK `tenants`; retrieval không bao giờ trả dữ liệu tenant khác (test).
2. Hybrid retrieval: lexical + vector + rerank; lọc `kinds/tags/as_of/require_verified`; budget token; conflict được liệt kê trong `ContextPacket.conflicts`.
3. Evidence store append-only (trigger như `audit_log`), artifact content-addressed sha256.
4. Learning: `OutcomeRecorder.record` tạo DatasetRecord đúng stage; `stats()` tính `cost_per_verified_success` khớp tổng cost.
5. Champion/challenger: hàm quyết định promote có ngưỡng + report; không promote R2+ tự động.
6. Eval runner chạy corpus ≥ 5 case × 18 family với fake/local, xuất report JSON.
7. Backup/restore script cho Brain DB + test restore so khớp (evidence `data_match`).

## TESTS
`tests/brain_learning/` (marker `pg` cho store). Bắt buộc marker `cloud_exit_project_brain` (retrieval + context packet trên PG thật,
không GPU) và `cloud_exit_evidence` (EvidenceRecord ghi/đọc PG, truy vết task → evidence → artifact).

## EVIDENCE
Dòng tổng kết pytest thật; output cloud_exit_check; eval report JSON; restore test log.

## MERGE CONTRACT
Chỉ owned paths; test PASS (không skip pg); PROJECT_BRAIN + EVIDENCE = PASS; không import implementation A/C/D;
`docs/reports/B/PHASE1.md` đủ mục; CONTRACT_CHANGE_REQUEST nếu cần.
