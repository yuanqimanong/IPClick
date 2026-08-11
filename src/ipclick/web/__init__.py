"""IPClick Web 管理端（``ipclick run --web``）。"""

from ipclick.web.auth import SessionStore, WebCredentials, announce, generate_password
from ipclick.web.server import COOKIE_NAME, WebConfig, WebServer
from ipclick.web.templates import render_dashboard, render_login


__all__ = [
    "COOKIE_NAME",
    "SessionStore",
    "WebConfig",
    "WebCredentials",
    "WebServer",
    "announce",
    "generate_password",
    "render_dashboard",
    "render_login",
]
