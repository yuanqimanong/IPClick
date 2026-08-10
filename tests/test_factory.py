"""[GENERAL].mode —— 单机 / 集群客户端的选择。

这个配置项从项目一开始就在，但一直没有消费方。这里验它真的生效了。
"""

import pytest

from ipclick.exceptions import ConfigError
from ipclick.factory import create_client, resolve_mode
from ipclick.utils.config_util import Settings


_NODES = {"nodes": [{"id": "a", "address": "127.0.0.1:9527"}]}


class TestResolveMode:
    def test_default_is_standalone(self):
        assert resolve_mode(Settings({})) == "standalone"

    def test_explicit_standalone(self):
        assert resolve_mode(Settings({"GENERAL": {"mode": "standalone"}})) == "standalone"

    def test_case_insensitive(self):
        assert resolve_mode(Settings({"GENERAL": {"mode": " Cluster "}, "CLUSTER": _NODES})) == "cluster"

    def test_cluster_with_nodes(self):
        assert resolve_mode(Settings({"GENERAL": {"mode": "cluster"}, "CLUSTER": _NODES})) == "cluster"

    def test_cluster_with_dns_discovery(self):
        config = Settings({"GENERAL": {"mode": "cluster"}, "CLUSTER": {"discovery": {"mode": "dns", "dns_name": "x"}}})
        assert resolve_mode(config) == "cluster"

    def test_cluster_without_nodes_raises(self):
        """静默退回单机会让你以为集群生效了，实际所有流量都打在一个节点上、
        也没有故障转移——这正是本项目一直在清理的那类"配了没生效"。"""
        with pytest.raises(ConfigError, match="既没有"):
            resolve_mode(Settings({"GENERAL": {"mode": "cluster"}}))

    def test_unknown_mode_raises(self):
        with pytest.raises(ConfigError, match=r"未知的 \[GENERAL\].mode"):
            resolve_mode(Settings({"GENERAL": {"mode": "swarm"}}))

    def test_auto_without_nodes_is_standalone(self):
        assert resolve_mode(Settings({"GENERAL": {"mode": "auto"}})) == "standalone"

    def test_auto_with_nodes_is_cluster(self):
        assert resolve_mode(Settings({"GENERAL": {"mode": "auto"}, "CLUSTER": _NODES})) == "cluster"


class TestCreateClient:
    def test_default_is_downloader(self, tmp_path):
        from ipclick.config_loader.loader import load_config
        from ipclick.sdk import Downloader

        cfg = tmp_path / "c.toml"
        cfg.write_text('[GENERAL]\nmode = "standalone"\n', encoding="utf-8")
        load_config.cache_clear()
        try:
            client = create_client(str(cfg))
            try:
                assert isinstance(client, Downloader)
            finally:
                client.close()
        finally:
            load_config.cache_clear()

    def test_cluster_mode_returns_cluster_client(self, tmp_path):
        from ipclick.cluster import ClusterDownloader
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[GENERAL]\nmode = "cluster"\n\n[CLUSTER]\nnodes = [{ id = "a", address = "127.0.0.1:19527" }]\n',
            encoding="utf-8",
        )
        load_config.cache_clear()
        try:
            client = create_client(str(cfg), start_probing=False)
            try:
                assert isinstance(client, ClusterDownloader)
                assert len(client.pool) == 1
            finally:
                client.close()
        finally:
            load_config.cache_clear()

    def test_cluster_mode_ignores_host_port(self, tmp_path):
        """集群模式下目标地址来自节点池，host/port 传了也没用——要提醒而不是默默吃掉。"""
        from ipclick.cluster import ClusterDownloader
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[GENERAL]\nmode = "cluster"\n\n[CLUSTER]\nnodes = [{ id = "a", address = "127.0.0.1:19527" }]\n',
            encoding="utf-8",
        )
        load_config.cache_clear()
        try:
            client = create_client(str(cfg), host="1.2.3.4", port=1234, start_probing=False)
            try:
                assert isinstance(client, ClusterDownloader)
            finally:
                client.close()
        finally:
            load_config.cache_clear()


class TestGlobalDownloader:
    """回归：全局 downloader / get_downloader 以前硬编码 Downloader。

    于是配了 mode = "cluster" 的人只要用 `from ipclick import downloader` 就会
    静默拿到单机客户端——所有流量打在一个节点上、没有故障转移，而 create_client()
    那边却明确拒绝这种静默降级。同一个配置项在两条路径上表现不同，比不支持还糟。
    """

    def test_get_downloader_honours_cluster_mode(self, tmp_path):
        from ipclick.cluster import ClusterDownloader
        from ipclick.config_loader.loader import load_config
        from ipclick.factory import get_downloader

        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[GENERAL]\nmode = "cluster"\n\n[CLUSTER]\nnodes = [{ id = "a", address = "127.0.0.1:19527" }]\n'
            "\n[CLUSTER.discovery]\nrefresh_interval = 0\n",
            encoding="utf-8",
        )
        load_config.cache_clear()
        try:
            assert isinstance(get_downloader(str(cfg)), ClusterDownloader)
        finally:
            load_config.cache_clear()

    def test_explicit_host_always_standalone(self, tmp_path):
        """点名了地址就别再去解释 mode——调用方要的就是这个节点。"""
        from ipclick.config_loader.loader import load_config
        from ipclick.factory import get_downloader
        from ipclick.sdk import Downloader

        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[GENERAL]\nmode = "cluster"\n\n[CLUSTER]\nnodes = [{ id = "a", address = "127.0.0.1:19527" }]\n',
            encoding="utf-8",
        )
        load_config.cache_clear()
        try:
            client = get_downloader(str(cfg), host="127.0.0.1", port=19999)
            assert isinstance(client, Downloader)
            assert client.port == 19999
        finally:
            load_config.cache_clear()

    def test_standalone_by_default(self):
        from ipclick.factory import get_downloader
        from ipclick.sdk import Downloader

        assert isinstance(get_downloader(), Downloader)

    def test_instances_are_cached(self):
        from ipclick.factory import get_downloader

        assert get_downloader() is get_downloader()
