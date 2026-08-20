"""Web 管理端的凭据解析、会话管理与登录限流。"""

from collections import deque
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
    """使用密码学安全随机源生成临时管理密码。"""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class WebCredentials:
    """Web 登录凭据，以及凭据是否由本次进程临时生成。"""

    username: str
    password: str
    generated: bool = False

    @classmethod
    def resolve(cls, web_config: dict[str, Any] | None) -> "WebCredentials":
        """按环境变量优先、配置文件次之的顺序解析登录凭据。"""
        config = dict(web_config or {})
        username = (os.getenv(ENV_USER) or str(config.get("username") or "")).strip() or "admin"
        password = (os.getenv(ENV_PASSWORD) or str(config.get("password") or "")).strip()
        if password:
            return cls(username=username, password=password, generated=False)
        return cls(username=username, password=generate_password(), generated=True)

    def verify(self, username: str, password: str) -> bool:
        """以恒定时间比较用户名和密码，降低时序侧信道风险。"""
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
    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0


class SessionStore:
    """线程安全的内存会话、CSRF token 与来源锁定状态存储。"""

    def __init__(self, ttl: float = DEFAULT_SESSION_TTL):
        self.ttl: float = ttl
        self._sessions: dict[str, _Session] = {}
        self._failures: dict[str, _Attempts] = {}
        self._lock: threading.Lock = threading.Lock()

    def create(self, username: str) -> tuple[str, str]:
        """创建会话，返回不可预测的 ``(session_id, csrf_token)``。"""
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_locked()
            self._sessions[session_id] = _Session(
                username=username, csrf_token=csrf_token, expires_at=time.monotonic() + self.ttl
            )
        return session_id, csrf_token

    def get(self, session_id: str | None) -> _Session | None:
        """读取未过期会话；发现过期记录时立即清理。"""
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
        """销毁会话；空值和未知会话视为幂等成功。"""
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def check_csrf(self, session_id: str | None, token: str | None) -> bool:
        """校验 token 是否属于指定的有效会话。"""
        session = self.get(session_id)
        if session is None or not token:
            return False
        return hmac.compare_digest(session.csrf_token.encode(), token.encode())

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._sessions.items() if v.expires_at <= now]:
            del self._sessions[key]

    def _prune_failures_locked(self, now: float) -> None:
        stale: list[str] = []
        for source, record in self._failures.items():
            while record.failures and now - record.failures[0] >= LOCKOUT_WINDOW:
                record.failures.popleft()
            if record.locked_until <= now and not record.failures:
                stale.append(source)
        for source in stale:
            del self._failures[source]

    def is_locked(self, source: str) -> float:
        """返回来源剩余锁定秒数，未锁定时返回 ``0.0``。"""
        with self._lock:
            now = time.monotonic()
            self._prune_failures_locked(now)
            record = self._failures.get(source)
            if record is None or record.locked_until <= now:
                return 0.0
            return record.locked_until - now

    def record_failure(self, source: str) -> None:
        """记录一次登录失败，并在滑动窗口内达到阈值后锁定来源。"""
        with self._lock:
            now = time.monotonic()
            # 顺手清理其他来源的旧记录，避免长期运行时被一次性失败来源撑大。
            self._prune_failures_locked(now)
            record = self._failures.setdefault(source, _Attempts())
            was_locked = record.locked_until > now
            record.failures.append(now)
            if len(record.failures) >= MAX_FAILED_ATTEMPTS:
                # 从触发阈值的最近一次失败开始计算完整锁定期，避免窗口末尾
                # 的第五次失败只锁几秒。
                record.locked_until = max(record.locked_until, now + LOCKOUT_WINDOW)
                if not was_locked:
                    log.warning(f"Web 登录失败次数过多，已锁定来源 {source} {LOCKOUT_WINDOW:.0f} 秒")

    def record_success(self, source: str) -> None:
        """登录成功后清除该来源累计的失败次数。"""
        with self._lock:
            self._failures.pop(source, None)

    def snapshot(self) -> dict[str, int]:
        """返回当前有效会话数和实际处于锁定状态的来源数。"""
        with self._lock:
            self._prune_locked()
            now = time.monotonic()
            self._prune_failures_locked(now)
            locked = sum(record.locked_until > now for record in self._failures.values())
            return {"active_sessions": len(self._sessions), "locked_sources": locked}


def announce(credentials: WebCredentials, url: str) -> None:
    """在启动日志中公布管理地址，并仅展示临时生成的密码。"""
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
