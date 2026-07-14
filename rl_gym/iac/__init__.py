"""IaC vertical — secure-Terraform generation scored by a security scanner.

`env.IacEnv` is the gym Environment (scan pass-rate = reward; gates = builds_required
+ no_critical); `scan` runs Checkov if installed, else a built-in rule set.
"""
from .env import IacEnv

__all__ = ["IacEnv"]
