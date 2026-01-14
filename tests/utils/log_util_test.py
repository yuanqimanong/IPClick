"""
example_usage.py - 展示如何使用日志工具类
"""

from concurrent.futures import ThreadPoolExecutor

from ipclick.utils.log_util import SQLiteAdapter, log

# log.init(
#     level="warning",
#     log_file="logs/app.txt",
#     rotation="10 KB",
#     retention="30 days",
# )

# sqlite_adapter = SQLiteAdapter("logs/app.db")
# log.init("debug", adapter=sqlite_adapter)


def test_log():
    log.debug("这是一条debug日志。")
    log.info("这是一条info日志。")
    log.warning("这是一条warning日志。")
    log.error("这是一条error日志。")
    log.critical("这是一条critical日志。")

    def func(a, b):
        return a / b

    def nested(c):
        try:
            func(5, c)
        except ZeroDivisionError:
            log.exception("What?!")

    nested(0)


def main():
    test_log()

    # print("使用ThreadPoolExecutor测试并发写入")
    # with ThreadPoolExecutor(
    #     max_workers=100, thread_name_prefix="LogWorker"
    # ) as executor:
    #     futures = []
    #     for thread_id in range(1000):
    #         future = executor.submit(test_log)
    #         futures.append(future)
    #
    #     # 等待所有线程完成
    #     for future in futures:
    #         future.result()
    #
    # print("ThreadPoolExecutor测试完成！\n")


if __name__ == "__main__":
    main()
