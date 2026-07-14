"""Environment registry — `--env <name>` resolves to an Environment factory.

Adding a task to the gym = register its Environment here; the trainer, eval, and
audit then work with `--env <name>` and need no task-specific code.
"""
from __future__ import annotations

from typing import Callable

from .core import Environment

_REGISTRY: dict[str, Callable[..., Environment]] = {}


def register(name: str, factory: Callable[..., Environment]) -> None:
    _REGISTRY[name] = factory


def get_env(name: str, **kwargs) -> Environment:
    if name not in _REGISTRY:
        raise SystemExit(f"unknown env '{name}'; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def available() -> list[str]:
    return sorted(_REGISTRY)


# --- built-in environments ---
def _iac(**kw) -> Environment:
    from ..iac.env import IacEnv
    return IacEnv(**kw)


def _iac_repair(**kw) -> Environment:
    from ..iac.repair import IacRepairEnv
    return IacRepairEnv(**kw)


register("iac", _iac)
register("iac_repair", _iac_repair)
