#!/usr/bin/env python
"""重新生成 task.proto 的 Python / gRPC 代码。

用法：
    uv run python -m ipclick.dto.proto.generate

grpc_tools 生成的 task_pb2_grpc.py 使用顶层导入 ``import task_pb2``，
在包内会 ImportError。以前这一行是每次手工改回相对导入的（见 task.proto
顶部的注释），本脚本把这一步固化下来，避免再次遗漏。
"""

from pathlib import Path
import subprocess
import sys


PROTO_DIR = Path(__file__).parent
PROTO_FILE = "task.proto"

# grpc_tools 生成的顶层导入 -> 包内相对导入
_ABSOLUTE_IMPORT = "import task_pb2 as task__pb2"
_RELATIVE_IMPORT = "from . import task_pb2 as task__pb2"


def generate() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            "-I.",
            "--python_out=.",
            "--grpc_python_out=.",
            "--pyi_out=.",
            PROTO_FILE,
        ],
        cwd=PROTO_DIR,
        check=True,
    )

    grpc_stub = PROTO_DIR / "task_pb2_grpc.py"
    source = grpc_stub.read_text(encoding="utf-8")
    if _RELATIVE_IMPORT not in source:
        if _ABSOLUTE_IMPORT not in source:
            raise RuntimeError(f"在 {grpc_stub} 中找不到预期的导入语句，请检查 grpc_tools 版本")
        grpc_stub.write_text(source.replace(_ABSOLUTE_IMPORT, _RELATIVE_IMPORT, 1), encoding="utf-8")

    print(f"已生成 {PROTO_FILE} 的代码并修正包内导入")


if __name__ == "__main__":
    generate()
