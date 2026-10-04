"""Đăng nhập tối thiểu + phiên + CSRF cho Workbench.

- Mật khẩu: băm PBKDF2-SHA256 ``pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>`` lấy từ env ``ZEUS_WB_PASSWORD_HASH``.
  Chưa cấu hình => KHÔNG đăng nhập được (fail closed). Tạo băm: ``python -m zeus.workbench.security 'mật-khẩu'``.
- Phiên: cookie HttpOnly SameSite=Strict, giá trị ký HMAC (khoá ``ZEUS_WB_SESSION_KEY``; mặc định ngẫu nhiên mỗi lần chạy).
- CSRF: token = HMAC(khoá, "csrf|" + sid) gắn với cookie sid (kể cả trước khi đăng nhập); mọi POST phải kèm token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field

PBKDF2_ITERATIONS = 240_000
SID_COOKIE = "zwb_sid"
AUTH_COOKIE = "zwb_auth"


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algo, it, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(it))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


@dataclass
class AuthConfig:
    password_hash: str | None = None
    username: str = "operator"
    session_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    session_ttl_s: int = 8 * 3600
    secure_cookies: bool = False  # bật khi chạy sau HTTPS
    max_failures: int = 5
    failure_window_s: int = 300
    _failures: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque), repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AuthConfig":
        e = os.environ if env is None else env
        kw: dict[str, object] = {"password_hash": e.get("ZEUS_WB_PASSWORD_HASH") or None, "username": e.get("ZEUS_WB_USER") or "operator"}
        if e.get("ZEUS_WB_SESSION_KEY"):
            kw["session_key"] = e["ZEUS_WB_SESSION_KEY"].encode()
        kw["secure_cookies"] = (e.get("ZEUS_WB_SECURE_COOKIES") or "").lower() in {"1", "true", "yes"}
        return cls(**kw)  # type: ignore[arg-type]

    # -- ký giá trị
    def _mac(self, msg: str) -> str:
        return hmac.new(self.session_key, msg.encode(), hashlib.sha256).hexdigest()

    def csrf_token(self, sid: str) -> str:
        return self._mac("csrf|" + sid)

    def check_csrf(self, sid: str, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf_token(sid), token or "")

    def make_auth_cookie(self, sid: str, user: str, now: float | None = None) -> str:
        exp = int((now or time.time()) + self.session_ttl_s)
        payload = f"{sid}|{user}|{exp}"
        return base64.urlsafe_b64encode(payload.encode()).decode() + "." + self._mac("auth|" + payload)

    def read_auth_cookie(self, value: str | None, sid: str | None, now: float | None = None) -> str | None:
        """Trả username nếu cookie hợp lệ, chưa hết hạn và gắn đúng sid; ngược lại None."""
        if not value or not sid or "." not in value:
            return None
        b64, mac = value.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(b64.encode()).decode()
            c_sid, user, exp = payload.split("|")
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(self._mac("auth|" + payload), mac) or c_sid != sid or int(exp) < (now or time.time()):
            return None
        return user

    # -- đăng nhập + giới hạn thử sai
    def locked(self, client: str, now: float | None = None) -> bool:
        q, t = self._failures[client], now or time.time()
        while q and q[0] < t - self.failure_window_s:
            q.popleft()
        return len(q) >= self.max_failures

    def login(self, client: str, username: str, password: str, now: float | None = None) -> bool:
        if self.locked(client, now):
            return False
        ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8")) & verify_password(password, self.password_hash)
        if not ok:
            self._failures[client].append(now or time.time())
        else:
            self._failures.pop(client, None)
        return bool(ok)


if __name__ == "__main__":  # pragma: no cover
    print(hash_password(sys.argv[1]) if len(sys.argv) > 1 else "usage: python -m zeus.workbench.security <password>")
