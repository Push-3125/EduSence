"""Execution and integration adapters for EduSense Orchestrator."""

from .base import AntigravityAdapter
from .antigravity import (
    AntigravitySDKAdapter,
    AntigravityCLIAdapter,
    SimulatedAdapter,
    InteractiveFallbackAdapter,
    get_antigravity_adapter,
)
from .github import GitHubAdapter
from .notifier import Notifier

__all__ = [
    "AntigravityAdapter",
    "AntigravitySDKAdapter",
    "AntigravityCLIAdapter",
    "SimulatedAdapter",
    "InteractiveFallbackAdapter",
    "get_antigravity_adapter",
    "GitHubAdapter",
    "Notifier",
]
