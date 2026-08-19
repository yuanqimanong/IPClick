from __future__ import annotations

from ipclick.web.templates.base import NAV, attr, esc, set_default_theme
from ipclick.web.templates.components import render_components
from ipclick.web.templates.config import render_config
from ipclick.web.templates.dashboard import dashboard_live, render_dashboard
from ipclick.web.templates.deploy import render_deploy
from ipclick.web.templates.login import render_login
from ipclick.web.templates.sandbox import render_test
from ipclick.web.templates.skill import render_skill
from ipclick.web.templates.trace import DEFAULT_LIVE_MS, LIVE_INTERVALS, live_label, render_trace, trace_live


__all__ = [
    "DEFAULT_LIVE_MS",
    "LIVE_INTERVALS",
    "NAV",
    "attr",
    "dashboard_live",
    "esc",
    "live_label",
    "render_components",
    "render_config",
    "render_dashboard",
    "render_deploy",
    "render_login",
    "render_skill",
    "render_test",
    "render_trace",
    "set_default_theme",
    "trace_live",
]
