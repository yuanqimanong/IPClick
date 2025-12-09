# -*- coding:utf-8 -*-

"""
@time: 2025-12-09
@author: Hades
@file: cli.py
"""

import click

from . import commands


@click.group()
def cli_app():
    """IPClick 命令行工具"""
    pass


# 添加命令
cli_app.add_command(commands.ping, name="ping")
cli_app.add_command(commands.scan, name="scan")
cli_app.add_command(commands.info, name="info")
