"""给子节点生成的部署材料。

守的是"复制过去就能起来"：节点列表和主控一致、self_id 是这一台、令牌两边对得上。
抄配置是组集群最容易出错的一步，而这三样错了分别表现为不分活、不分活、
UNAUTHENTICATED——都不好查。
"""

from __future__ import annotations

import io
import tomllib
import zipfile

import pytest

from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT
from ipclick.web.deploy import build_plan, bundle, node_commands, node_env, node_toml


NODES = [
    {"id": "node-a", "address": "10.0.0.1:9528"},
    {"id": "node-b", "address": "10.0.0.2:9528"},
]


class TestToml:
    def test_is_valid_toml(self):
        parsed = tomllib.loads(node_toml(node_id="node-a", port=9528, web_port=9527, nodes=NODES, forward=True))
        assert parsed["SERVER"]["port"] == 9528
        assert parsed["WEB"]["port"] == 9527

    def test_node_list_matches_the_master(self):
        """ "任意节点都能当入口"的前提就是每台都有完整且相同的列表。"""
        parsed = tomllib.loads(node_toml(node_id="node-a", port=9528, web_port=9527, nodes=NODES, forward=True))
        assert [n["address"] for n in parsed["CLUSTER"]["nodes"]] == ["10.0.0.1:9528", "10.0.0.2:9528"]

    def test_self_id_is_the_only_per_machine_difference(self):
        a = tomllib.loads(node_toml(node_id="node-a", port=9528, web_port=9527, nodes=NODES, forward=True))
        b = tomllib.loads(node_toml(node_id="node-b", port=9528, web_port=9527, nodes=NODES, forward=True))
        assert a["CLUSTER"]["self_id"] == "node-a"
        assert b["CLUSTER"]["self_id"] == "node-b"
        assert a["CLUSTER"]["nodes"] == b["CLUSTER"]["nodes"]

    def test_forward_is_a_string_not_a_bool(self):
        """ClusterConfig 认的是 "on"/"off"，写成 true 会被当成关闭。"""
        parsed = tomllib.loads(node_toml(node_id="a", port=1, web_port=2, nodes=NODES, forward=True))
        assert parsed["CLUSTER"]["forward"] == "on"
        parsed = tomllib.loads(node_toml(node_id="a", port=1, web_port=2, nodes=NODES, forward=False))
        assert parsed["CLUSTER"]["forward"] == "off"

    def test_listens_on_all_interfaces(self):
        """子节点要被主控从别的机器连到，只监听 127.0.0.1 就白搭了。"""
        parsed = tomllib.loads(node_toml(node_id="a", port=1, web_port=2, nodes=NODES, forward=True))
        assert parsed["SERVER"]["host"] == "[::]"

    def test_parses_with_the_real_cluster_config(self):
        """生成的东西必须能被本项目自己的解析器吃下去。"""
        from ipclick.cluster.node import ClusterConfig

        parsed = tomllib.loads(node_toml(node_id="node-a", port=9528, web_port=9527, nodes=NODES, forward=True))
        cluster = ClusterConfig.from_config(parsed["CLUSTER"])
        assert [n.id for n in cluster.nodes] == ["node-a", "node-b"]
        assert cluster.forwarding_enabled is True


class TestEnv:
    def test_carries_both_secrets(self):
        text = node_env(auth_token="tok-123", cluster_secret="sec-456")
        assert "IPCLICK_AUTH_TOKEN=tok-123" in text
        assert "IPCLICK_CLUSTER_SECRET=sec-456" in text

    def test_parses_with_the_real_dotenv_parser(self):
        from ipclick.config_loader.dotenv import parse_env

        parsed = parse_env(node_env(auth_token="tok-123", cluster_secret="sec-456"))
        assert parsed["IPCLICK_AUTH_TOKEN"] == "tok-123"
        assert parsed["IPCLICK_CLUSTER_SECRET"] == "sec-456"

    def test_says_to_chmod(self):
        assert "600" in node_env(auth_token="a", cluster_secret="b")


class TestCommands:
    """三条踩过的坑，都是这一组在守。"""

    def _commands(self) -> dict[str, str]:
        return dict(node_commands(port=19001, version="0.5.0"))

    def test_three_ways(self):
        """uv 建的 venv 默认不装 pip；很多机器上又没有 uv；而自己 build 的版本
        两条都装不到，只能从主控拷 wheel。"""
        commands = self._commands()
        assert set(commands) == {"pip", "uv", "本地 wheel（版本没发布到 PyPI 时用这条）"}

    def test_version_is_pinned(self):
        """不钉版本的话 `uv pip install ipclick` 会去 PyPI 抓"最新的"——
        实测装到的是旧版，起来之后行为和主控对不上。"""
        for name, command in self._commands().items():
            assert "0.5.0" in command, name
            # 光写 ipclick 而不带 ==版本 的地方一处都不该有
            assert "install ipclick " not in command + " ", name

    def test_never_uses_uv_run(self):
        """uv run 会重新解析依赖，可能装到别的地方去；而且这里目录里没有
        pyproject.toml，它的语义并不是"跑我刚装的那个"。"""
        assert all("uv run" not in command for command in self._commands().values())

    def test_runs_the_venv_binary_directly(self):
        assert all(".venv/bin/ipclick run" in command for command in self._commands().values())

    def test_command_pins_the_port(self):
        """--port 让它去读 ipclick-<端口>.toml。"""
        assert all("--port 19001" in command for command in self._commands().values())

    def test_wheel_variant_mentions_copying_it_over(self):
        wheel = self._commands()["本地 wheel（版本没发布到 PyPI 时用这条）"]
        assert "ipclick-0.5.0-py3-none-any.whl" in wheel
        assert "scp" in wheel


class TestPlan:
    @staticmethod
    def _plan(address: str = "10.0.0.1:9528"):
        return build_plan(
            {"id": "node-a", "address": address},
            nodes=NODES,
            forward=True,
            auth_token="tok",
            cluster_secret="sec",
        )

    def test_default_port_keeps_the_familiar_pair(self):
        """默认端口的部署保持 9528/9527 那一对——文档和启动横幅里反复出现的
        就是它，换掉只会多一个要记的数。"""
        plan = self._plan(f"10.0.0.1:{DEFAULT_GRPC_PORT}")
        assert plan.web_port == DEFAULT_WEB_PORT

    def test_web_port_never_collides_with_another_nodes_grpc_port(self):
        """0.5.0 之前是 ``web = grpc - 1``。配上「添加节点」连续分配的
        19001/19002/19003，生成出 19000/19001/19002——node-2 的 Web 端口正好
        等于 node-1 的 gRPC 端口。同机部署直接起不来，跨机则是凭空又造一批
        "这个端口是谁的"。
        """
        nodes = [{"id": f"node-{i}", "address": f"10.0.0.{i}:{19000 + i}"} for i in (1, 2, 3)]
        plans = [
            build_plan(n, nodes=nodes, forward=True, auth_token="t", cluster_secret="s", version="0.5.0") for n in nodes
        ]
        grpc_ports = {p.port for p in plans}
        web_ports = [p.web_port for p in plans]
        assert not (set(web_ports) & grpc_ports), "Web 端口撞上了某台的 gRPC 端口"
        assert len(set(web_ports)) == len(web_ports), "两台节点分到了同一个 Web 端口"

    def test_web_port_dodges_an_explicitly_listed_grpc_port(self):
        """偏移量算出来的值正好被某台占了时要让开，而不是照发。"""
        nodes = [{"id": "a", "address": "10.0.0.1:19001"}, {"id": "b", "address": "10.0.0.2:29001"}]
        plan = build_plan(nodes[0], nodes=nodes, forward=True, auth_token="t", cluster_secret="s", version="0.5.0")
        assert plan.web_port != 29001
        assert plan.web_port not in {19001, 29001}

    def test_filename_prefix_is_safe(self):
        """节点 id 可能是 host:port 形式，冒号在 Windows 上不是合法文件名。"""
        plan = build_plan(
            {"id": "10.0.0.1:9528", "address": "10.0.0.1:9528"},
            nodes=NODES,
            forward=False,
            auth_token="t",
            cluster_secret="s",
        )
        assert ":" not in plan.filename_prefix

    def test_snapshot_has_everything_the_page_needs(self):
        snapshot = self._plan().snapshot()
        assert snapshot["toml"] and snapshot["env"]
        assert len(snapshot["commands"]) == 3
        assert snapshot["toml_name"].startswith("ipclick-")

    def test_toml_is_named_by_port(self):
        """几台节点的配置要能并排放在同一个目录里而不打架——忘记建子目录的后果
        是第二台默默覆盖第一台。"""
        assert self._plan("10.0.0.1:19002").toml_name == "ipclick-19002.toml"

    @pytest.mark.parametrize("address", ["10.0.0.1:9528", "bad-address"])
    def test_never_raises_on_odd_addresses(self, address: str):
        """这是个生成器，不是校验器。地址奇怪时给个能看的结果，不要炸。"""
        assert self._plan(address).toml


class TestBundle:
    def test_zip_has_a_directory_per_node(self):
        plans = [build_plan(node, nodes=NODES, forward=True, auth_token="t", cluster_secret="s") for node in NODES]
        with zipfile.ZipFile(io.BytesIO(bundle(plans))) as archive:
            names = archive.namelist()
            assert "node-a/ipclick-9528.toml" in names
            assert "node-b/.env" in names
            assert "README.txt" in names
            # 解压出来的东西必须自解释，否则过两周没人记得哪个目录对应哪台机器
            readme = archive.read("README.txt").decode()
            assert "10.0.0.1:9528" in readme
            assert "chmod 600" in readme

    def test_empty_cluster_still_produces_a_readme(self):
        with zipfile.ZipFile(io.BytesIO(bundle([]))) as archive:
            assert archive.namelist() == ["README.txt"]
