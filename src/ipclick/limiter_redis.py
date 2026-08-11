"""按 host 限流的 Redis 后端（跨进程 / 跨节点共享额度）。

:mod:`ipclick.limiter` 的内存后端是**每个进程各算各的**。集群里 N 个节点就是
N 倍的实际并发——``per_host_max_concurrent = 4`` 配在 5 个节点上，目标站点看到
的是 20 个并发。这个后端把计数放到 Redis 上，让整个集群共用一份额度。

两把锁都用 Lua 实现，因为"读-判断-写"必须是原子的：分成多条命令的话，
两个节点会同时读到"还有 1 个名额"然后各拿一个。

崩溃了怎么办
------------
分布式信号量最经典的坑：进程拿着名额挂了，名额永远还不回来，那个 host 从此
被锁死。这里给每个持有者打上时间戳（ZSET 的 score），取名额之前先把超过
``slot_ttl`` 的陈旧条目清掉。代价是**单个请求超过 slot_ttl 时它的名额会被别人
抢走**，短暂超出上限——所以 ``slot_ttl`` 要配得比最长的请求还长。
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import os
import time
from typing import Any
import uuid

from ipclick.exceptions import ConfigError
from ipclick.limiter import HostLimitTimeout, LimiterSettings, host_of
from ipclick.utils.log_util import log


_redis: Any

try:
    import redis as _redis
except ImportError:  # pragma: no cover - 取决于安装环境
    _redis = None

REDIS_AVAILABLE: bool = _redis is not None

#: 键前缀，方便在共享 Redis 上和别的业务区分开
KEY_PREFIX = "ipclick:limit"

#: 取并发名额。KEYS[1]=zset，ARGV=now_ms, ttl_ms, limit, token
#: 必须原子：分成多条命令的话，两个节点会同时读到"还有名额"然后各拿一个。
_ACQUIRE_SLOT = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - ttl)
if redis.call('ZCARD', KEYS[1]) < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ttl)
  return 1
end
return 0
"""

#: 取一个令牌。KEYS[1]=hash，ARGV=now_ms, qps, burst, ttl_ms
#: 返回还需等待的毫秒数；0 表示拿到了。
_TAKE_TOKEN = """
local now = tonumber(ARGV[1])
local qps = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + ((now - ts) / 1000.0) * qps)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = math.ceil(((1 - tokens) / qps) * 1000)
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
return wait
"""


@dataclass(frozen=True)
class RedisSettings:
    """来自 ``[DOWNLOADER.rate_limit]`` 里 backend = "redis" 时的那几项。"""

    url: str = "redis://127.0.0.1:6379/0"
    #: 持有者被判为陈旧的时长（秒）。必须大于最长的单次请求，
    #: 否则慢请求的名额会被别人抢走，短暂超出上限。
    slot_ttl: float = 600.0
    #: 连接与命令超时（秒）。Redis 抖动不该把业务请求拖死。
    socket_timeout: float = 3.0
    key_prefix: str = KEY_PREFIX

    @classmethod
    def from_config(cls, rate_config: dict[str, Any] | None) -> "RedisSettings":
        config = dict(rate_config or {})
        defaults = cls()

        def _float(key: str, fallback: float) -> float:
            try:
                value = float(config.get(key))  # pyright: ignore[reportArgumentType]
            except (TypeError, ValueError):
                return fallback
            return value if value > 0 else fallback

        return cls(
            url=(os.getenv("IPCLICK_REDIS_URL") or str(config.get("redis_url") or "")).strip() or defaults.url,
            slot_ttl=_float("redis_slot_ttl", defaults.slot_ttl),
            socket_timeout=_float("redis_socket_timeout", defaults.socket_timeout),
            key_prefix=str(config.get("redis_key_prefix") or defaults.key_prefix).strip() or defaults.key_prefix,
        )


class RedisHostLimiter:
    """与 :class:`~ipclick.limiter.HostLimiter` 同样的接口，但额度存在 Redis 上。

    ``acquire()`` 的语义完全一致，调用方（TaskService）不需要区分后端。
    """

    def __init__(
        self,
        settings: LimiterSettings | None = None,
        redis_settings: RedisSettings | None = None,
        *,
        client: Any = None,
    ):
        if client is None and _redis is None:
            raise ConfigError('分布式限流需要 redis 库：pip install "ipclick[redis]"')

        self.settings: LimiterSettings = settings or LimiterSettings()
        self.redis_settings: RedisSettings = redis_settings or RedisSettings()

        if client is not None:
            self._client: Any = client
        else:
            self._client = _redis.Redis.from_url(
                self.redis_settings.url,
                socket_timeout=self.redis_settings.socket_timeout,
                socket_connect_timeout=self.redis_settings.socket_timeout,
                decode_responses=False,
            )
        self._acquire_script: Any = self._client.register_script(_ACQUIRE_SLOT)
        self._token_script: Any = self._client.register_script(_TAKE_TOKEN)

    # ------------------------------------------------------------------ #
    # 额度
    # ------------------------------------------------------------------ #

    @contextmanager
    def acquire(self, url: str, timeout: float | None = None) -> Generator[None]:
        settings = self.settings
        host = host_of(url) if settings.enabled else ""
        if not host:
            yield
            return

        deadline = time.monotonic() + (settings.wait_timeout if timeout is None else max(0.0, timeout))
        token = uuid.uuid4().hex
        holding = False

        if settings.per_host_max_concurrent > 0:
            holding = self._acquire_slot(host, token, deadline)
        try:
            self._take_token(host, deadline)
            yield
        finally:
            if holding:
                self._release_slot(host, token)

    def _slot_key(self, host: str) -> str:
        return f"{self.redis_settings.key_prefix}:slots:{host}"

    def _token_key(self, host: str) -> str:
        return f"{self.redis_settings.key_prefix}:tokens:{host}"

    def _acquire_slot(self, host: str, token: str, deadline: float) -> bool:
        ttl_ms = int(self.redis_settings.slot_ttl * 1000)
        # 轮询而不是阻塞等待：Redis 没有跨客户端的"信号量可用"通知，
        # BLPOP 那套要维护一个额外的队列键，故障恢复更麻烦。
        # 50ms 的轮询间隔对"每秒几十个请求"这个量级完全够用。
        while True:
            ok, got = self._call(
                self._acquire_script,
                keys=[self._slot_key(host)],
                args=[int(time.time() * 1000), ttl_ms, self.settings.per_host_max_concurrent, token],
            )
            if not ok:
                # Redis 不可用：放行，且不要记成"持有名额"（没什么可归还的）
                return False
            if got:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostLimitTimeout(
                    f"等待 {host} 的并发额度超时（集群上限 {self.settings.per_host_max_concurrent} 个并发，"
                    f"已等待 {self.settings.wait_timeout:.1f} 秒）"
                )
            time.sleep(min(0.05, remaining))

    def _release_slot(self, host: str, token: str) -> None:
        try:
            self._client.zrem(self._slot_key(host), token)
        except Exception as e:
            # 还不回去也不致命：ZSET 上的时间戳会让它在 slot_ttl 后自动过期。
            # 但要留下日志，否则"额度慢慢变少"会查不出原因。
            log.warning(f"归还 {host} 的并发额度失败（{e}），将在 {self.redis_settings.slot_ttl:.0f} 秒后自动过期")

    def _take_token(self, host: str, deadline: float) -> None:
        qps = self.settings.per_host_qps
        if qps <= 0:
            return
        ttl_ms = int(max(self.redis_settings.slot_ttl, 60.0) * 1000)
        while True:
            ok, value = self._call(
                self._token_script,
                keys=[self._token_key(host)],
                args=[int(time.time() * 1000), qps, self.settings.burst, ttl_ms],
            )
            if not ok:
                return  # Redis 不可用：放行
            wait_ms = int(value or 0)
            if wait_ms <= 0:
                return
            wait = wait_ms / 1000.0
            if time.monotonic() + wait > deadline:
                raise HostLimitTimeout(
                    f"等待 {host} 的速率额度超时（集群上限 {qps:g} QPS，已等待 {self.settings.wait_timeout:.1f} 秒）"
                )
            time.sleep(wait)

    def _call(self, script: Any, *, keys: list[str], args: list[Any]) -> tuple[bool, Any]:
        """执行 Lua 脚本。返回 ``(Redis 是否可用, 返回值)``。

        必须把"Redis 挂了"和"脚本返回 0"分开——两者都用 0 表示的话，
        一次 Redis 故障会被当成"没有名额"，然后一路轮询到超时，
        最终把所有请求拒掉。那和放行是完全相反的行为。
        """
        try:
            return True, script(keys=keys, args=args)
        except Exception as e:
            # Redis 不可用时**放行**而不是拒绝所有请求。限流是保护性措施，
            # 让它的故障演变成全站不可用是本末倒置；打日志让人能发现。
            log.error(f"Redis 限流后端不可用，本次请求不受限流约束：{e}")
            return False, None

    def snapshot(self) -> dict[str, Any]:
        return {
            "backend": "redis",
            "enabled": self.settings.enabled,
            "per_host_max_concurrent": self.settings.per_host_max_concurrent,
            "per_host_qps": self.settings.per_host_qps,
            "redis_url": self.redis_settings.url,
        }


def build_limiter(downloader_config: dict[str, Any] | None) -> Any:
    """按 ``[DOWNLOADER.rate_limit].backend`` 造出对应的限流器。

    未知的 backend 值直接报错——静默回退到内存后端的话，集群里以为开了共享
    限额、实际每个节点各算各的，问题要到把对方站点打挂才会暴露。
    """
    from ipclick.limiter import HostLimiter

    config = dict(downloader_config or {})
    rate = dict(config.get("rate_limit") or {})
    backend = str(rate.get("backend") or "memory").strip().lower()
    settings = LimiterSettings.from_config(config)

    if backend in ("", "memory", "local"):
        return HostLimiter(settings)
    if backend == "redis":
        if not settings.enabled:
            log.warning("[DOWNLOADER.rate_limit].backend = redis，但并发与 QPS 上限都是 0，限流不会生效")
        return RedisHostLimiter(settings, RedisSettings.from_config(rate))
    raise ConfigError(f"未知的限流后端 {backend!r}，可选：memory、redis")


__all__ = ["KEY_PREFIX", "REDIS_AVAILABLE", "RedisHostLimiter", "RedisSettings", "build_limiter"]
