"""解析日志等运行文件的相对路径并创建父目录。"""

import os
from pathlib import Path


DEFAULT_LOG_FILENAME = "ipclick.log"


class PathUtil:
    """无状态的文件路径辅助方法集合。"""

    @staticmethod
    def resolve_path(path: str | Path, base_dir: Path | None = None) -> Path:
        """相对 ``base_dir``（缺省为当前目录）解析路径。"""
        path_obj = Path(path)

        if path_obj.is_absolute():
            return path_obj

        if base_dir is None:
            base_dir = Path.cwd()

        return base_dir / path_obj

    @staticmethod
    def looks_like_directory(path: str | Path) -> bool:
        """根据尾部分隔符判断尚不存在的路径是否表示目录。"""
        raw = str(path)
        if not raw:
            return False
        separators = (os.sep, os.altsep) if os.altsep else (os.sep,)
        return raw.endswith(separators)

    @staticmethod
    def resolve_log_file(
        path: str | Path,
        base_dir: Path | None = None,
        *,
        default_name: str = DEFAULT_LOG_FILENAME,
    ) -> Path:
        """把目录补成默认日志文件，并为无扩展名文件补 ``.log``。"""
        resolved = PathUtil.resolve_path(path, base_dir)
        if PathUtil.looks_like_directory(path) or resolved.is_dir():
            return resolved / default_name
        return resolved if resolved.suffix else resolved.with_suffix(".log")

    @staticmethod
    def ensure_parent_dir(path: str | Path) -> None:
        """递归创建目标文件的父目录。"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)


__all__ = ["DEFAULT_LOG_FILENAME", "PathUtil"]
