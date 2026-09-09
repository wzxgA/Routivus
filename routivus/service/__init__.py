"""Programmatic backend services for Routivus.

UI-free orchestration and command entry points for desktop clients.
"""

from __future__ import annotations

from routivus.service.commands import (
    _attach_model,
    _cmd_config,
    _cmd_hitl,
    _cmd_model,
    _cmd_smart_router,
    _disable_smart_router,
    _memory_manager,
    _model_catalog,
    _reapply_active,
    _switch,
    handle_service_command,
)

__all__ = [
    "handle_service_command",
    "_cmd_model",
    "_cmd_smart_router",
    "_cmd_config",
    "_cmd_hitl",
    "_model_catalog",
    "_switch",
    "_reapply_active",
    "_attach_model",
    "_disable_smart_router",
    "_memory_manager",
]