"""
example_usage.py - 展示如何使用日志工具类
"""

import time
import random
from pathlib import Path

from ipclick.utils.log_util import SQLiteAdapter, LogUtil, log

# 方案1：在应用入口初始化一次
# LogUtil.init(
#     log_file="logs/app.log",  # 相对路径
#     base_dir=Path(__file__).parent,  # 基于当前文件目录
#     level="DEBUG",
#     use_rich=True,  # 自动检测rich是否可用
# )

# 添加数据库适配器（可选）
# db_adapter = SQLiteAdapter("logs/app.db")
# LogUtil.add_database_adapter(db_adapter)


# 方案2：使用全局别名
log.info("应用程序启动")


@LogUtil.catch_errors("risky_operation", capture_locals=True)
class DataProcessor:
    """数据处理类示例。"""

    def __init__(self, name: str):
        self.name = name
        self.data = []
        log.info(f"初始化处理器: {name}")

    def safe_operation(self):
        """安全操作，不会被catch_errors装饰。"""
        log.info("执行安全操作")
        return "success"

    def risky_operation(self, param: str):
        """风险操作，会被catch_errors装饰。"""
        log.debug(f"执行风险操作，参数: {param}")

        # 模拟可能失败的操作
        if random.random() < 0.3:
            raise ValueError(f"处理失败: {param}")

        result = f"处理结果: {param.upper()}"
        log.info(f"操作成功: {result}")
        return result

    @log.performance(threshold=0.5)
    def slow_operation(self):
        """可能较慢的操作。"""
        time.sleep(random.uniform(0.1, 1.0))
        return "完成"

    def process_with_context(self, data: list):
        """使用上下文管理器处理数据。"""
        with log.catch("处理数据失败", capture_locals=True):
            log.info(f"开始处理 {len(data)} 条数据")

            # 这里可能发生异常
            if not data:
                raise ValueError("数据为空")

            # 模拟处理过程
            for i, item in enumerate(data):
                log.debug(f"处理第 {i + 1} 条数据: {item}")

            return f"处理了 {len(data)} 条数据"


def main():
    """主函数示例。"""
    processor = DataProcessor("示例处理器")

    # 测试各种日志场景
    log.debug("调试信息", extra={"app": "demo"})
    log.info("一般信息")
    log.warning("警告信息")

    # 测试异常记录
    try:
        processor.risky_operation("test")
    except Exception:
        pass  # 异常已被记录

    # 测试性能监控
    processor.slow_operation()

    # 测试上下文管理器
    try:
        processor.process_with_context([])
    except Exception:
        pass

    # 测试带上下文变量的错误记录
    try:
        x = 42
        y = "hello"
        raise RuntimeError("测试异常")
    except Exception as e:
        log.error(
            "测试带上下文的错误",
            exception=e,
            context_vars={"x": x, "y": y, "list_data": [1, 2, 3]},
        )

    log.info("应用程序结束")


if __name__ == "__main__":
    main()
