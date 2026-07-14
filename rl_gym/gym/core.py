"""RL Gym core — the tiny verifiable-reward interface every task plugs into.

The thesis: an agent task becomes RL-trainable the moment you can write a
**verifiable** reward for it. This module is that contract. An `Environment`
turns a task into `prompt -> completion -> reward`, where the reward is COMPOSED
from calculable primitives:

  * `Component` — a soft, weighted objective in [0, 1] (nutrition closeness,
    budget fit, test-pass rate, row-match F1, ...). Blended by weight.
  * `Gate`     — a hard, safety-critical constraint (allergen-free, SQL parses,
    no forbidden API). Any failing gate zeroes the reward. Never learned.

Composing from a vetted primitive library — instead of free-form reward code — is
the platform's safety property: the reward stays calculable, gameable-resistant,
and **auditable** (raw per-component scores are always exposed, so the
reward-hacking audit can see if the policy maxes one cheap term or collapses).
It's also what lets a natural-language builder assemble a reward *from primitives*
without reintroducing the Goodhart problem RLVR exists to avoid.

Swap the `Environment` to retarget the gym; the SFT/GRPO trainer, the eval
harness, and the audit are reused unchanged. `iac.env.IacEnv` is the reference
implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

# A task defines its own episode (request + candidate pool, SQL schema + question,
# a coding problem, ...) and its own parsed action (a plan, a query, a diff, ...).
Episode = Any
Action = Any


@dataclass
class RewardBreakdown:
    """What every reward returns: the scalar GRPO optimizes, plus the **raw**
    per-component scores and gate results the audit reads (never the blended or
    saturated values — honesty is the point)."""
    reward: float                       # final scalar in [0, 1]
    components: dict[str, float]        # raw per-primitive scores in [0, 1]
    gates: dict[str, bool]             # hard pass/fail
    valid: bool                         # did the completion parse to an action?
    action: Action = None


@dataclass
class Component:
    """A soft, calculable objective in [0, 1]; `weight` is its blend share."""
    name: str
    weight: float
    fn: Callable[[Episode, Action], float]


@dataclass
class Gate:
    """A hard constraint: any False zeroes the reward. Deterministic, never learned."""
    name: str
    fn: Callable[[Episode, Action], bool]


def _saturate(x: float, s: float) -> float:
    """Diminishing-returns cap: identity at s>=1; else map [0,s]->[0,1], clamp >s.
    Use with care — capping already-bounded components can flatten the reward and
    (with reward-std normalization) cause collapse. Off by default."""
    return x if s >= 1.0 else min(x, s) / s


@dataclass
class Reward:
    """A verifiable reward = weighted `Component`s + hard `Gate`s + a format bonus.

    `score` is the single place the scalar is assembled, so every task gets the
    same audit-friendly contract: blended soft objectives, hard gates that zero
    the reward, a small bonus for a well-formed action, a penalty for a malformed
    one.
    """
    components: Sequence[Component]
    gates: Sequence[Gate] = field(default_factory=tuple)
    format_bonus: float = 0.05
    format_penalty: float = 0.0
    saturate: float = 1.0

    def score(self, episode: Episode, action: Optional[Action]) -> RewardBreakdown:
        if action is None:
            return RewardBreakdown(self.format_penalty, {}, {}, False, None)
        comps = {c.name: max(0.0, min(1.0, c.fn(episode, action))) for c in self.components}
        gates = {g.name: bool(g.fn(episode, action)) for g in self.gates}
        passed = all(gates.values()) if gates else True
        blend = sum(c.weight * _saturate(comps[c.name], self.saturate) for c in self.components)
        reward = 0.0 if not passed else min(1.0, blend + self.format_bonus)
        return RewardBreakdown(reward, comps, gates, True, action)


@runtime_checkable
class Environment(Protocol):
    """The plug-in point. Implement these and the trainer + eval + audit just work.

    name      : short id for logging / registry.
    reward    : the composed verifiable reward.
    episodes  : task instances for a split ("train" / "dev").
    prompt    : render an episode to the text the policy sees.
    parse     : turn a raw completion into an action, or None if malformed.
    oracle    : a reference 'best-effort' completion — the eval ceiling.
    random    : a random reference completion — the eval floor.
    """
    name: str
    reward: Reward

    def episodes(self, split: str) -> list[Episode]: ...
    def prompt(self, episode: Episode) -> str: ...
    def parse(self, completion: str, episode: Episode) -> Optional[Action]: ...
    def oracle(self, episode: Episode) -> str: ...
    def random(self, episode: Episode) -> str: ...


def score_completion(env: Environment, completion: str, episode: Episode) -> RewardBreakdown:
    """Bridge used by the trainer / eval / audit: parse then score. The GRPO reward
    function is just `score_completion(env, c, ep).reward` over a batch."""
    return env.reward.score(episode, env.parse(completion, episode))
