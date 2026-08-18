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
    # 多台机器共用同一份 ipclick.toml 时，唯一必须逐机不同的就是节点身份，
    # 所以它必须能从环境变量给。
    "IPCLICK_CLUSTER_SELF_ID": ("CLUSTER", "self_id", str),
    # 鉴权令牌与集群共享密钥由 ipclick.auth / ipclick.cluster.tokens 单独处理
    # （前者要支持多令牌，后者要参与派生），这里不重复
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


def candidate_names(port: int | None = None) -> list[str]:
    """自动查找时按什么顺序试文件名。

    带 ``--port`` 时**先**找 ``ipclick-<端口>.toml``。这是为"一台机器上起多个实例"
    准备的：``ipclick run --port 8001`` 与 ``--port 8002`` 各读各的配置，于是两者
    可以有不同的 worker 数、不同的限流、不同的链路库。

    0.4 只能靠 ``-c`` 一个个指过去，而那要求每次启动都记得带上——漏一次的症状是
    两个实例共用一份配置、往同一个 trace 库里写，界面上完全看不出来。

    找不到按端口命名的那个就回落到 ``ipclick.toml``，所以单实例部署一个字都不用改。
    """
    names: list[str] = []
    if port:
        names += [f"ipclick-{port}.toml", f".ipclick-{port}.toml"]
    return [*names, "ipclick.toml", ".ipclick.toml"]


@lru_cache(maxsize=8)
def load_config(config_path: str | Path | None = None, port: int | None = None) -> Settings:
    # .env 先加载：它只填补尚未设置的环境变量，之后 ENV_OVERRIDES 统一读取。
    load_dotenv()

    config_list: list[Any] = [DEFAULT_CONFIG_PATH, HOME_CONFIG_PATH]

    if config_path:
        user_path = Path(config_path)
    else:
        # 自动查找常见文件名（带端口的优先，见 candidate_names）
        for name in candidate_names(port):
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


def example_config() -> str:
    """随包分发的默认配置全文，供 ``ipclick --example`` 输出。"""
    return DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


def example_env() -> str:
    """``.env`` 模板。内容由 :mod:`ipclick.secrets` 生成——只列机密。

    非机密的部署参数（``IPCLICK_HOST`` 等）仍然支持，但刻意不进这个模板：
    ``.env`` 是放密钥的文件，部署参数属于容器编排注入的范畴。
    """
    from ipclick.secrets import env_template

    return env_template()


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ENV_OVERRIDES",
    "HOME_CONFIG_PATH",
    "candidate_names",
    "example_config",
    "example_env",
    "load_config",
]
