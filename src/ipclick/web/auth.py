"""Web 端的凭据、会话与登录限速。

这个服务本身就能代任意 URL 发请求。给它开一个网页界面，等于把一个高价值目标
暴露在通常比 gRPC 端口设防更少的地方——所以这里的每一处都按"会被人认真打"来写：

* 密码用 :func:`hmac.compare_digest` 比对，不给计时侧信道。
* 会话 ID 用 :func:`secrets.token_urlsafe`，不是可预测的计数器。
* 登录失败按来源 IP 限速，挡住在线撞库。
* 每个会话带 CSRF token；所有改变状态的请求都要校验。

没配密码怎么办
--------------
**生成一个随机密码打印到控制台**，而不是用 admin/admin 之类的默认值，也不是
干脆不要密码。默认弱口令是这类管理界面被打穿的头号原因——一旦有默认值，
总会有人原样部署到公网上。
"""

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
    """生成随机密码。

    只用字母数字：这串东西要从控制台复制粘贴，掺进标点会在各种终端和 shell 里
    被转义、被截断，反而让人绕过它去设个弱口令。
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class WebCredentials:
    """Web 端登录凭据。"""

    username: str
    password: str
    generated: bool = False

    @classmethod
    def resolve(cls, web_config: dict[str, Any] | None) -> "WebCredentials":
        """按 环境变量 > 配置文件 > 现生成 的顺序确定凭据。

        环境变量优先是因为密码不该写进会进版本库的配置文件；
        ``.env`` 已经在 load_config 阶段注入过环境变量了。
        """
        config = dict(web_config or {})
        username = (os.getenv(ENV_USER) or str(config.get("username") or "")).strip() or "admin"
        password = (os.getenv(ENV_PASSWORD) or str(config.get("password") or "")).strip()
        if password:
            return cls(username=username, password=password, generated=False)
        return cls(username=username, password=generate_password(), generated=True)

    def verify(self, username: str, password: str) -> bool:
        """常量时间比对。

        两个字段都要比，且**都要走 compare_digest**——先判用户名再判密码的话，
        响应时间会泄漏"用户名对不对"。
        """
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
    """内存会话表。

    刻意不做持久化：重启后所有人重新登录是完全可接受的，而把会话写盘等于
    多一处需要保护的机密。
    """

    def __init__(self, ttl: float = DEFAULT_SESSION_TTL):
        self.ttl: float = ttl
        self._sessions: dict[str, _Session] = {}
        self._failures: dict[str, _Attempts] = {}
        self._lock: threading.Lock = threading.Lock()

    def create(self, username: str) -> tuple[str, str]:
        """新建会话，返回 ``(session_id, csrf_token)``。"""
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
        """返回该来源还需锁定多少秒；0 表示未锁定。"""
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
    """把登录地址与凭据打印到控制台。

    密码只在**本次现生成**时打印。配置里自己设的密码不重复打出来——
    日志经常被收集到集中式系统里，往那儿抄一份长期有效的口令没有必要。
    """
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
