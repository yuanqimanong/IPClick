from dataclasses import dataclass, field
import hmac
import os
import secrets
import string
import threading
import time
from typing import Any

from ipclick.utils.log_util import log


DEFAULT_SESSION_TTL = 12 * 3600

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW = 300.0

GENERATED_PASSWORD_LENGTH = 20

ENV_USER = "IPCLICK_WEB_USER"
ENV_PASSWORD = "IPCLICK_WEB_PASSWORD"

_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = GENERATED_PASSWORD_LENGTH) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class WebCredentials:
    username: str
    password: str
    generated: bool = False

    @classmethod
    def resolve(cls, web_config: dict[str, Any] | None) -> "WebCredentials":
        config = dict(web_config or {})
        username = (os.getenv(ENV_USER) or str(config.get("username") or "")).strip() or "admin"
        password = (os.getenv(ENV_PASSWORD) or str(config.get("password") or "")).strip()
        if password:
            return cls(username=username, password=password, generated=False)
        return cls(username=username, password=generate_password(), generated=True)

    def verify(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(username.encode(), self.username.encode())
        pass_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return user_ok and pass_ok


@dataclass
class _Session:
    username: str
    csrf_token: str
    expires_at: float


@dataclass
class _Attempts:
    count: int = 0
    first_at: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, ttl: float = DEFAULT_SESSION_TTL):
        self.ttl: float = ttl
        self._sessions: dict[str, _Session] = {}
        self._failures: dict[str, _Attempts] = {}
        self._lock: threading.Lock = threading.Lock()

    def create(self, username: str) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._sessions[session_id] = _Session(
                username=username, csrf_token=csrf_token, expires_at=time.monotonic() + self.ttl
            )
        return session_id, csrf_token

    def get(self, session_id: str | None) -> _Session | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= time.monotonic():
                del self._sessions[session_id]
                return None
            return session

    def destroy(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def check_csrf(self, session_id: str | None, token: str | None) -> bool:
        session = self.get(session_id)
        if session is None or not token:
            return False
        return hmac.compare_digest(session.csrf_token.encode(), token.encode())

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._sessions.items() if v.expires_at <= now]:
            del self._sessions[key]

    def is_locked(self, source: str) -> float:
        with self._lock:
            record = self._failures.get(source)
            if record is None or record.count < MAX_FAILED_ATTEMPTS:
                return 0.0
            elapsed = time.monotonic() - record.first_at
            if elapsed >= LOCKOUT_WINDOW:
                del self._failures[source]
                return 0.0
            return LOCKOUT_WINDOW - elapsed

    def record_failure(self, source: str) -> None:
        with self._lock:
            record = self._failures.get(source)
            now = time.monotonic()
            if record is None or now - record.first_at >= LOCKOUT_WINDOW:
                self._failures[source] = _Attempts(count=1, first_at=now)
            else:
                record.count += 1
                if record.count == MAX_FAILED_ATTEMPTS:
                    log.warning(f"Web 登录失败次数过多，已锁定来源 {source} {LOCKOUT_WINDOW:.0f} 秒")

    def record_success(self, source: str) -> None:
        with self._lock:
            self._failures.pop(source, None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"active_sessions": len(self._sessions), "locked_sources": len(self._failures)}


def announce(credentials: WebCredentials, url: str) -> None:
    lines = ["", "=" * 62, f"  IPClick Web 管理端: {url}", f"  用户名: {credentials.username}"]
    if credentials.generated:
        lines += [
            f"  密码:   {credentials.password}",
            "",
            "  ⚠️ 该密码为本次启动随机生成，重启后失效。",
            f"     要固定下来，请设置环境变量 {ENV_USER} / {ENV_PASSWORD}，",
            "     或在配置文件 [WEB] 里填写（更推荐前者——密码不该进版本库）。",
        ]
    else:
        lines.append("  密码:   （取自环境变量或配置文件，此处不再打印）")
    lines += ["=" * 62, ""]
    for line in lines:
        log.info(line) if line.strip() else print()
    print("\n".join(lines))


__all__ = [
    "DEFAULT_SESSION_TTL",
    "ENV_PASSWORD",
    "ENV_USER",
    "LOCKOUT_WINDOW",
    "MAX_FAILED_ATTEMPTS",
    "SessionStore",
    "WebCredentials",
    "announce",
    "generate_password",
]
