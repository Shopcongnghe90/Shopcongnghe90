"""Local AI runtime (workstream C): client llama.cpp server (OpenAI-compatible), cấu hình GPU, benchmark.

Hệ thống phải chạy khi GPU vắng: mọi lỗi kết nối/health => ``LocalAIUnavailable`` (broker fallback), không crash.
"""

from zeus.localai.client import LocalAIUnavailable, LlamaServerClient

__all__ = ["LlamaServerClient", "LocalAIUnavailable"]
