# -*- coding:utf-8 -*-

"""
@time: 2025-12-09
@author: Hades
@file: commands.py
"""

import platform
import socket

import click


@click.command()
@click.argument('ip')
@click.option('--count', default=4, help='发送次数')
def ping(ip, count):
    """模拟 ping 命令"""
    click.echo(f"Pinging {ip} with {count} packets...")
    for i in range(1, count + 1):
        click.echo(f"Reply from {ip}: bytes=32 time={i * 10}ms TTL=64")
    click.echo(f"\nPing statistics for {ip}:")
    click.echo(f"Packets: Sent = {count}, Received = {count}, Lost = 0 (0% loss)")


@click.command()
@click.argument('ip_range', default="192.168.1.0/24")
@click.option('--timeout', default=0.5, help='超时时间(秒)')
def scan(ip_range, timeout):
    """扫描网络设备"""
    click.echo(f"Scanning network {ip_range}...")

    # 模拟扫描结果
    devices = [
        {"ip": "192.168.1.1", "hostname": "router", "status": "online"},
        {"ip": "192.168.1.10", "hostname": "pc-01", "status": "online"},
        {"ip": "192.168.1.15", "hostname": "printer", "status": "offline"},
    ]

    click.echo("\nFound devices:")
    click.echo("IP Address\tHostname\tStatus")
    click.echo("-" * 40)
    for device in devices:
        status = click.style("online", fg="green") if device["status"] == "online" else click.style("offline", fg="red")
        click.echo(f"{device['ip']}\t{device['hostname']}\t{status}")


@click.command()
@click.option('--detailed', is_flag=True, help='显示详细信息')
def info(detailed):
    """显示系统信息"""
    hostname = socket.gethostname()
    ip_address = socket.gethostbyname(hostname)
    os_info = platform.platform()

    click.echo(f"Hostname: {hostname}")
    click.echo(f"IP Address: {ip_address}")
    click.echo(f"OS: {os_info}")

    if detailed:
        click.echo("\nDetailed Information:")
        click.echo(f"Processor: {platform.processor()}")
        click.echo(f"Architecture: {platform.architecture()[0]}")
        click.echo(f"Python Version: {platform.python_version()}")
