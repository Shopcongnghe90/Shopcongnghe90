"""Provider adapters: AnthropicProvider (SDK chính thức ``anthropic``), OpenAICompatibleProvider (httpx;
OpenAI / Gemini OpenAI-compat / llama.cpp local chỉ khác base_url + key), và FakeProvider (test/cloud-exit).

Provider chỉ nhận ModelRequest đã được broker chuẩn bị (system tách riêng, role user/assistant, PII đã che nếu cần).
Provider KHÔNG tự đọc biến môi trường ngoài key_env được cấu hình, không log nội dung/khoá.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from zeus.broker.config import ModelSpec
from zeus.contracts.interfaces import ProviderUnavailable
from zeus.contracts.models import Cost, ModelRequest, ModelResponse, ProviderCapability, ProviderKind, Usage

JSON_HINT = "Chỉ trả về MỘT JSON object hợp lệ, không kèm giải thích hay code fence."


class ProviderRequestError(RuntimeError):
    """Provider từ chối yêu cầu này (400, refusal...). Khác với mất dịch vụ: không làm provider bị coi là down."""


def _system_text(request: ModelRequest) -> str | None:
    parts = [p for p in (request.system, JSON_HINT if request.json_output else None) if p]
    return "\n\n".join(parts) if parts else None


def _merge_turns(request: ModelRequest) -> list[dict[str, str]]:
    """Gộp lượt liên tiếp cùng role; tool -> user. (system đã được broker tách ra.)"""
    out: list[dict[str, str]] = []
    for m in request.messages:
        role = "assistant" if m.role == "assistant" else "user"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + m.content
        else:
            out.append({"role": role, "content": m.content})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(bắt đầu)"})
    return out


class AnthropicProvider:
    kind = ProviderKind.ANTHROPIC

    def __init__(self, api_key: str | None = None, *, client: Any = None, models: Sequence[ModelSpec] = (), base_url: str | None = None, timeout: float = 90.0) -> None:
        self._api_key = api_key
        self._client = client
        self._base_url = base_url
        self._timeout = timeout
        self._models = [m for m in models if m.provider is ProviderKind.ANTHROPIC]

    @property
    def available(self) -> bool:
        return bool(self._client is not None or self._api_key)

    def capabilities(self) -> list[ProviderCapability]:
        return [m.capability(self.available) for m in self._models]

    async def health(self) -> bool:
        return self.available

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise ProviderUnavailable("anthropic: thiếu API key")
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key, base_url=self._base_url, timeout=self._timeout, max_retries=1)
        return self._client

    async def complete(self, request: ModelRequest, model: str) -> ModelResponse:
        import anthropic

        client = self._get_client()
        kwargs: dict[str, Any] = {"model": model, "max_tokens": request.max_tokens, "messages": _merge_turns(request)}
        system = _system_text(request)
        if system:
            kwargs["system"] = system
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        t0 = time.monotonic()
        try:
            resp = await client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            raise ProviderRequestError(f"anthropic 400: {getattr(exc, 'message', exc)}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderUnavailable(f"anthropic auth: {type(exc).__name__}") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderUnavailable(f"anthropic model/endpoint not found: {model}") from exc
        except anthropic.APIStatusError as exc:  # RateLimit(429), Overloaded(529), Internal(5xx), ...
            if exc.status_code == 429 or exc.status_code >= 500 or exc.status_code == 408:
                raise ProviderUnavailable(f"anthropic status {exc.status_code}: {type(exc).__name__}") from exc
            raise ProviderRequestError(f"anthropic status {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:  # gồm APITimeoutError
            raise ProviderUnavailable(f"anthropic connection: {type(exc).__name__}") from exc
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        if resp.stop_reason == "refusal" and not text:
            raise ProviderRequestError("anthropic refusal")
        u = resp.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return ModelResponse(
            request_id=request.request_id,
            provider=ProviderKind.ANTHROPIC,
            model=model,
            text=text,
            usage=usage,
            latency_ms=int((time.monotonic() - t0) * 1000),
            finish_reason=resp.stop_reason,
        )


class OpenAICompatibleProvider:
    """``POST {base_url}/chat/completions``. Dùng cho OpenAI (kind=OPENAI), Gemini compat (GEMINI), llama.cpp (LOCAL)."""

    def __init__(
        self,
        kind: ProviderKind,
        base_url: str | None,
        api_key: str | None = None,
        *,
        models: Sequence[ModelSpec] = (),
        client: httpx.AsyncClient | None = None,
        require_key: bool = True,
        timeout: float = 90.0,
        max_tokens_field: str = "max_tokens",
    ) -> None:
        self.kind = kind
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._require_key = require_key
        self._client = client
        self._timeout = timeout
        self._max_tokens_field = max_tokens_field
        self._models = [m for m in models if m.provider is kind]

    @property
    def available(self) -> bool:
        return bool(self._base_url) and (bool(self._api_key) or not self._require_key)

    def capabilities(self) -> list[ProviderCapability]:
        return [m.capability(self.available) for m in self._models]

    async def health(self) -> bool:
        return self.available

    async def complete(self, request: ModelRequest, model: str) -> ModelResponse:
        if not self.available:
            raise ProviderUnavailable(f"{self.kind.value}: chưa cấu hình base_url/key")
        msgs: list[dict[str, str]] = []
        system = _system_text(request)
        if system:
            msgs.append({"role": "system", "content": system})
        msgs += _merge_turns(request)
        body: dict[str, Any] = {"model": model, self._max_tokens_field: request.max_tokens, "messages": msgs}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.json_output:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        t0 = time.monotonic()
        try:
            r = await client.post(f"{self._base_url}/chat/completions", json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailable(f"{self.kind.value} connection: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if r.status_code in (401, 403, 404, 408, 429) or r.status_code >= 500:
            raise ProviderUnavailable(f"{self.kind.value} status {r.status_code}")
        if r.status_code >= 400:
            raise ProviderRequestError(f"{self.kind.value} status {r.status_code}")
        try:
            data = r.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable(f"{self.kind.value}: phản hồi không đúng định dạng chat/completions") from exc
        u = data.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("prompt_tokens", 0) or 0), output_tokens=int(u.get("completion_tokens", 0) or 0))
        return ModelResponse(
            request_id=request.request_id,
            provider=self.kind,
            model=data.get("model") or model,
            text=text,
            usage=usage,
            latency_ms=int((time.monotonic() - t0) * 1000),
            finish_reason=choice.get("finish_reason"),
        )


class FakeProvider:
    """Provider giả cho test/cloud-exit: script quyết định nội dung; ``fail`` mô phỏng mất dịch vụ."""

    def __init__(
        self,
        kind: ProviderKind = ProviderKind.FAKE,
        *,
        models: Sequence[ModelSpec] = (),
        script: Callable[[ModelRequest], str] | None = None,
        fail: bool = False,
        fail_times: int = 0,
    ) -> None:
        self.kind = kind
        self.script = script
        self.fail = fail  # tắt hẳn (available=False)
        self.fail_times = fail_times  # còn "available" nhưng N lần gọi đầu bị ProviderUnavailable
        self._models = [m for m in models if m.provider is kind]
        self.calls: list[ModelRequest] = []

    @property
    def available(self) -> bool:
        return not self.fail

    def capabilities(self) -> list[ProviderCapability]:
        return [m.capability(self.available) for m in self._models]

    async def health(self) -> bool:
        return self.available

    async def complete(self, request: ModelRequest, model: str) -> ModelResponse:
        self.calls.append(request)
        if self.fail or self.fail_times > 0:
            self.fail_times = max(0, self.fail_times - 1)
            raise ProviderUnavailable(f"{self.kind.value}:{model} unavailable (fake)")
        text = self.script(request) if self.script else "{}"
        usage = Usage(input_tokens=sum(len(m.content) for m in request.messages) // 4 + 1, output_tokens=len(text) // 4 + 1)
        return ModelResponse(request_id=request.request_id, provider=self.kind, model=model, text=text, usage=usage, cost=Cost(), latency_ms=1, finish_reason="stop")
