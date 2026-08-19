import os
from pathlib import Path


DEFAULT_LOG_FILENAME = "ipclick.log"


class PathUtil:
    @staticmethod
    def resolve_path(path: str | Path, base_dir: Path | None = None) -> Path:
        path_obj = Path(path)

        if path_obj.is_absolute():
            return path_obj

        if base_dir is None:
            base_dir = Path.cwd()

        return base_dir / path_obj

    @staticmethod
    def looks_like_directory(path: str | Path) -> bool:
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
        resolved = PathUtil.resolve_path(path, base_dir)
        if PathUtil.looks_like_directory(path) or resolved.is_dir():
            return resolved / default_name
        return resolved if resolved.suffix else resolved.with_suffix(".log")

    @staticmethod
    def ensure_parent_dir(path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)


__all__ = ["DEFAULT_LOG_FILENAME", "PathUtil"]
