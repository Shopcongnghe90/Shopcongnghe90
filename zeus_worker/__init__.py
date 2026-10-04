"""zeus_worker — thin worker chạy trên VM (Owner: workstream C).

Phase 0 chỉ tạo khung gói. Worker nói HTTP với Worker API (zeus.contracts.api.Paths.WORKER_*),
không giữ API key provider, không cài LLM, không phụ thuộc dịch vụ Claude Cloud.
"""

__version__ = "0.1.0"
