"""Application composition roots shared by non-GUI adapters."""

from .agent_factory import build_agent_application, load_agent_config

__all__ = ["build_agent_application", "load_agent_config"]
