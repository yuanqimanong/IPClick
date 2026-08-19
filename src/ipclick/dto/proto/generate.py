from pathlib import Path
import subprocess
import sys


PROTO_DIR = Path(__file__).parent
PROTO_FILE = "task.proto"

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
