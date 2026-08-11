"""配置加载器。

**优先级（高 -> 低）**：

1. 命令行参数 / 构造函数参数（由调用方自己覆盖，不在本模块内）
2. 环境变量
3. 当前工作目录的 ``.env``（不覆盖已存在的环境变量）
4. 显式指定的配置文件，或当前目录的 ``ipclick.toml`` / ``.ipclick.toml``
5. ``~/.ipclick/config.toml``
6. 包内默认配置

``.env`` 排在真实环境变量之后是有意的：容器编排、CI、systemd 注入的变量必须能
压过仓库里那个用于本地开发的 ``.env``。
"""

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from ipclick.config_loader.dotenv import load_dotenv
from ipclick.utils.config_util import ConfigUtil, Settings


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "configs" / "default_config.toml"
HOME_CONFIG_PATH = Path.home() / ".ipclick" / "config.toml"

#: 环境变量 -> 配置路径。值为 ``(节, 键, 类型)``。
#:
#: 集中放一张表而不是散在各处 ``os.getenv``：散着写的话，"到底哪些环境变量有用"
#: 只能靠翻代码，文档也必然和实现失步。
ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "IPCLICK_HOST": ("SERVER", "host", str),
    "IPCLICK_PORT": ("SERVER", "port", int),
    "IPCLICK_MAX_WORKERS": ("SERVER", "max_workers", int),
    "IPCLICK_MODE": ("GENERAL", "mode", str),
    "IPCLICK_LOG_LEVEL": ("LOG", "level", str),
    # 鉴权令牌由 ipclick.auth 单独处理（要支持多令牌），这里不重复
}


def _apply_env_overrides(config: Settings) -> None:
    for name, (section, key, caster) in ENV_OVERRIDES.items():
        raw = os.getenv(name)
        if raw is None or raw == "":
            continue
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            # 环境变量写错了不该让服务起不来，但要留痕——否则"我明明设了 PORT"
            # 会查很久。这里用 print 而不是 log：日志系统本身要等配置才能初始化。
            print(f"[ipclick] 忽略非法的环境变量 {name}={raw!r}（期望 {caster.__name__}）")
            continue
        config.setdefault(section, {})[key] = value


@lru_cache(maxsize=3)
def load_config(config_path: str | Path | None = None) -> Settings:
    # .env 先加载：它只填补尚未设置的环境变量，之后 ENV_OVERRIDES 统一读取。
    load_dotenv()

    config_list: list[Any] = [DEFAULT_CONFIG_PATH, HOME_CONFIG_PATH]

    if config_path:
        user_path = Path(config_path)
    else:
        # 自动查找常见文件名
        for name in ["ipclick.toml", ".ipclick.toml"]:
            if Path(name).exists():
                user_path = Path(name)
                break
        else:
            user_path = None

    if user_path and user_path.exists():
        config_list.append(user_path)

    config = ConfigUtil.load(config_list)
    _apply_env_overrides(config)
    return config


#: 不在 ENV_OVERRIDES 表里、但确实被读取的环境变量。
#: 它们各自有特殊处理（多令牌、凭据解析），不适合走那张通用映射表，
#: 但**必须出现在 .env 模板里**——否则模板给人的印象就是"只有这几个能配"。
_EXTRA_ENV_DOCS: list[tuple[str, str]] = [
    ("IPCLICK_AUTH_TOKEN", "gRPC 鉴权令牌。留空 = 不鉴权（服务端会打显著告警）"),
    ("IPCLICK_WEB_USER", "Web 管理端用户名，默认 admin"),
    ("IPCLICK_WEB_PASSWORD", "Web 管理端密码。留空则每次启动随机生成并打印到控制台"),
]

_ENV_DOCS: dict[str, str] = {
    "IPCLICK_HOST": "服务端监听地址（客户端则是要连的地址）",
    "IPCLICK_PORT": "服务端端口",
    "IPCLICK_MAX_WORKERS": "gRPC worker 线程数",
    "IPCLICK_MODE": "客户端运行模式：standalone / cluster / auto",
    "IPCLICK_LOG_LEVEL": "日志级别：debug / info / warn / error",
}


def example_config() -> str:
    """随包分发的默认配置全文，供 ``ipclick --example`` 输出。"""
    return DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


def example_env() -> str:
    """``.env`` 模板，供 ``ipclick --example env`` 输出。

    **从 ENV_OVERRIDES 表生成**而不是手写一份：手写的模板迟早和实现失步，
    而"模板里有但其实不生效"比没有模板更误导人。
    """
    lines = [
        "# IPClick 环境变量模板",
        "#",
        "# 复制成 .env 放在启动 ipclick 的目录下（只在当前工作目录查找，不向上递归）。",
        "#",
        "# 优先级（高 -> 低）：",
        "#   命令行参数 > 真实环境变量 > .env > 配置文件 > 默认值",
        "#",
        "# .env 排在真实环境变量之后是有意的：容器编排 / CI / systemd 注入的变量",
        "# 必须能压过仓库里这份用于本地开发的 .env。",
        "#",
        "# 留空 = 不设置（会回落到配置文件或默认值），所以可以整份复制过去再按需填。",
        "#",
        "# ⚠️ .env 里会有令牌和密码，务必加进 .gitignore。",
        "",
    ]
    for name in ENV_OVERRIDES:
        lines.append(f"# {_ENV_DOCS.get(name, '')}".rstrip())
        lines.append(f"{name}=")
        lines.append("")
    for name, doc in _EXTRA_ENV_DOCS:
        lines.append(f"# {doc}")
        lines.append(f"{name}=")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ENV_OVERRIDES",
    "HOME_CONFIG_PATH",
    "example_config",
    "example_env",
    "load_config",
]
