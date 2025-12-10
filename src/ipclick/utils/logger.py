# -*- coding:utf-8 -*-

"""
日志工厂类

@time: 2025-12-10
@author: Hades
@file: logger.py
"""

import logging
import sys

from logging.handlers import RotatingFileHandler


class LoggerFactory:
    @staticmethod
    def setup_logging(config):
        """配置全局日志系统"""
        log_config = config.get('LOG', {})
        log_level = log_config.get('level', 'INFO').upper()
        log_format = log_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        log_output = log_config.get('output', 'stdout')
        rotation_config = log_config.get('rotation', {})

        # 创建格式化器
        formatter = logging.Formatter(log_format)

        # 创建处理器
        if log_output == 'stdout':
            handler = logging.StreamHandler(sys.stdout)
        else:
            handler = RotatingFileHandler(
                filename=log_output,
                maxBytes=rotation_config.get('max_size', 100) * 1024 * 1024,
                backupCount=rotation_config.get('max_backups', 5)
            )

        handler.setFormatter(formatter)

        # 配置根记录器
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # 移除现有处理器
        for h in root_logger.handlers[:]:
            root_logger.removeHandler(h)

        # 添加新处理器
        root_logger.addHandler(handler)

        # 减少第三方库日志输出
        # logging.getLogger("grpc").setLevel(logging.WARNING)
        # logging.getLogger("httpx").setLevel(logging.INFO)

        return root_logger
