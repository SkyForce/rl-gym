"""RL Gym — a verifiable-reward interface any task plugs into (trainer/eval/audit reused).

See core.py for the contract (Environment, Reward = Components + Gates) and
iac/env.py for the reference implementation.
"""
from .core import (
    Environment, Reward, Component, Gate, RewardBreakdown, score_completion,
)

__all__ = [
    "Environment", "Reward", "Component", "Gate", "RewardBreakdown", "score_completion",
]
