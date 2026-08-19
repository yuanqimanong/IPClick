from dataclasses import dataclass
import os
from typing import Any

from ipclick.utils.log_util import log


@dataclass(frozen=True)
class SecretSpec:
    env: str
    section: str
    key: str
    label: str
    generatable: bool = False
    shared: bool = False
    note: str = ""


SECRETS: tuple[SecretSpec, ...] = (
    SecretSpec(
        "IPCLICK_AUTH_TOKEN",
        "SECURITY",
        "auth_token",
        "gRPC 鉴权令牌",
        generatable=True,
        note="调用方要带同一个令牌（authorization: Bearer <令牌>）。轮换期间可以在 [SECURITY].auth_token 里写成数组让新旧并存。",
    ),
    SecretSpec("IPCLICK_WEB_USER", "WEB", "username", "Web 管理端用户名"),
    SecretSpec(
        "IPCLICK_WEB_PASSWORD",
        "WEB",
        "password",
        "Web 管理端密码",
        generatable=True,
        note="只影响本机的这个管理端，改完重启即可生效。",
    ),
    SecretSpec("IPCLICK_PROXY_AUTH_KEY", "PROXY", "auth_key", "代理账号"),
    SecretSpec("IPCLICK_PROXY_AUTH_PASSWORD", "PROXY", "auth_password", "代理密码"),
    SecretSpec(
        "IPCLICK_CLUSTER_SECRET",
        "CLUSTER",
        "secret",
        "集群共享密钥",
        generatable=True,
        shared=True,
        note=(
            "每个节点的令牌由它派生，所以**所有节点必须是同一个值**。"
            "在一台机器上生成，再原样复制到其余每台的 .env——各自生成一个就全对不上了。"
        ),
    ),
)

SUPPRESS_KEY = "allow_secrets_in_config"


def _dig(config: Any, path: str) -> dict[str, Any]:
    node: Any = config
    for part in path.split("."):
        if not isinstance(node, dict):
            node = dict(node or {})
        node = dict(node or {}).get(part) or {}
    return dict(node or {})


def config_value(config: Any, spec: SecretSpec) -> Any:
    return _dig(config, spec.section).get(spec.key)


def resolve(config: Any, spec: SecretSpec) -> tuple[str | None, str]:
    from_env = (os.getenv(spec.env) or "").strip()
    if from_env:
        return from_env, "env"
    raw = config_value(config, spec)
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), "config"
    if isinstance(raw, (list, tuple)) and raw:
        return str(raw[0]), "config"
    return None, "unset"


def describe_source(config: Any, spec: SecretSpec) -> str:
    _, origin = resolve(config, spec)
    if origin == "env":
        return "环境变量 / .env"
    if origin == "unset":
        return "未配置"
    return "配置文件 ⚠️ 建议改用环境变量"


def audit(config: Any) -> list[tuple[SecretSpec, str]]:
    return [(spec, resolve(config, spec)[1]) for spec in SECRETS]


def warn_secrets_in_config(config: Any) -> list[SecretSpec]:
    found: list[SecretSpec] = []
    for spec in SECRETS:
        raw = config_value(config, spec)
        if not raw:
            continue
        found.append(spec)

    if not found:
        return []

    suppressed = bool(_dig(config, "SECURITY").get(SUPPRESS_KEY, False))
    if suppressed:
        log.debug(f"配置文件中有 {len(found)} 项机密，已按 {SUPPRESS_KEY} 抑制警告")
        return found

    names = "、".join(f"[{s.section}].{s.key}" for s in found)
    envs = "、".join(s.env for s in found)
    log.warning(
        f"以下机密写在配置文件里：{names}。"
        f"ipclick.toml 通常是要进版本库的，机密会跟着进 git、备份和 CI 日志——"
        f"建议改用环境变量或 .env（{envs}）。"
        f"受信环境里确实想这么放的话，设置 [SECURITY].{SUPPRESS_KEY} = true 可关闭本提示"
    )
    return found


def proxy_config(config: Any) -> dict[str, Any]:
    merged = dict(_dig(config, "PROXY"))
    for spec in SECRETS:
        if spec.section != "PROXY":
            continue
        from_env = (os.getenv(spec.env) or "").strip()
        if from_env:
            merged[spec.key] = from_env
    return merged


def env_template() -> str:
    lines = [
        "# IPClick 机密配置",
        "#",
        "# 这个文件**只放机密**：凭据、密钥、带密码的连接串。",
        "# 行为配置（超时、重试、限流、浏览器引擎…）请写 ipclick.toml——",
        "# 那个文件应该进版本库，这个绝对不该。",
        "#",
        "# 放在启动 ipclick 的目录下（只在当前工作目录查找，不向上递归）。",
        "# 优先级：命令行 > 真实环境变量 > .env > 配置文件 > 默认值。",
        "#",
        "# 留空 = 不设置。文件权限建议 600（ipclick init 会自动设好）。",
        "#",
        "# 部署参数（IPCLICK_HOST / PORT / MAX_WORKERS / MODE / LOG_LEVEL /",
        "# CLUSTER_SELF_ID）同样支持，但那些不是机密，是给容器编排注入的，",
        "# 刻意不预置在这里——见 README。",
        "",
    ]
    for spec in SECRETS:
        lines.append(f"# {spec.label}")
        lines.append(f"{spec.env}=")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "SECRETS",
    "SUPPRESS_KEY",
    "SecretSpec",
    "audit",
    "config_value",
    "describe_source",
    "env_template",
    "proxy_config",
    "resolve",
    "warn_secrets_in_config",
]
