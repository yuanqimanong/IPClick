import os
from pathlib import Path


DEFAULT_LOG_FILENAME = "ipclick.log"


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

        if base_dir is None:
            base_dir = Path.cwd()

        return base_dir / path_obj

    @staticmethod
    def looks_like_directory(path: str | Path) -> bool:
        """这个路径**写法上**是不是一个目录（以路径分隔符结尾）。

        刻意只看字符串、不碰文件系统：``output = "logs/"`` 在目录还没建出来的
        时候同样应该被当成目录。"目录已经存在"是另一条独立的判据，由调用方
        自己加上——两条合起来才覆盖全部情况。

        分隔符取 :data:`os.sep` / :data:`os.altsep`，即 POSIX 上只认 ``/``，
        Windows 上 ``\\`` 与 ``/`` 都认。不无条件认反斜杠是有意的：POSIX 上
        它是合法的文件名字符，认了就等于禁掉一类合法路径。
        """
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
        """把 ``[LOG].output`` 解析成真正要写的**文件**路径。

        ``output`` 有两种合法写法，必须区分开：

        * **目录**（``logs/``，或一个已存在的目录）→ 在它**里面**用默认文件名。
        * **文件**（``logs/app.log``）→ 就是那个文件；缺扩展名时补 ``.log``。

        这个区分是补的一个静默 bug：旧实现无条件把 ``output`` 当文件路径，
        ``logs/`` 因为"没有扩展名"被 ``with_suffix(".log")`` 把最后一段整个换掉，
        日志落在了 ``logs.log``——和目标目录**同级**，目录里空空如也，而且不报
        任何错。日志是排障的基础设施，在这里静默配错的代价远大于别处。
        """
        resolved = PathUtil.resolve_path(path, base_dir)
        if PathUtil.looks_like_directory(path) or resolved.is_dir():
            return resolved / default_name
        return resolved if resolved.suffix else resolved.with_suffix(".log")

    @staticmethod
    def ensure_parent_dir(path: str | Path) -> None:
        """确保路径的父目录存在，不存在则创建

        Args:
            path: 文件路径

        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)


__all__ = ["DEFAULT_LOG_FILENAME", "PathUtil"]
