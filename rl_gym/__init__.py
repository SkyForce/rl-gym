"""RL Gym for Agents — verifiable-reward RL (SFT + GRPO) with a reward-hacking audit,
retargetable to any task by swapping the Environment.

Core: ``gym/`` (Environment = Components + Gates, trainer, eval, audit, registry).
Vertical: the ``iac`` secure-Terraform environment — the whole machine retargets to
any verifiable-reward task by swapping the Environment. The package root stays
dependency-light — import a vertical's API from its subpackage, e.g.
``from rl_gym.iac.env import IacEnv``.
"""
