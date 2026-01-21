from pathlib import Path


class PathUtil:
    """路径处理工具类"""

    @staticmethod
    def resolve_path(path: str | Path, base_dir: Path | None = None) -> Path:
        """解析文件路径，支持相对路径和绝对路径

        Args:
            path: 文件路径，可以是相对或绝对
            base_dir: 基础目录，用于解析相对路径

        Returns:
            Path: 解析后的绝对路径
        """
        path_obj = Path(path)

        if path_obj.is_absolute():
            return path_obj

        # 相对路径需要base_dir
        if base_dir is None:
            base_dir = Path.cwd()

        return base_dir / path_obj

    @staticmethod
    def ensure_parent_dir(path: str | Path) -> None:
        """确保路径的父目录存在，不存在则创建

        Args:
            path: 文件路径

        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
