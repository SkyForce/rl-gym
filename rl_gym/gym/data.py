"""Generic GRPO data layer: any Environment -> a TRL dataset + reward function.

Episodes stay native Python objects (no serialization): the dataset carries an
integer `ep_id`, and the reward fn closes over the episode list and looks them up.
That works for any episode type (an IaC request+required-types, SQL schema+question, a coding
problem) — the task never has to teach the gym how to (de)serialize itself.
"""
from __future__ import annotations

from typing import Optional

from .core import Environment, score_completion


def build_dataset(env: Environment, split: str, limit: int = 0,
                  keep_idx: Optional[list] = None):
    """Return (HF Dataset with `prompt` + `ep_id`, the episode list to close over).
    `keep_idx`: optional index subset into env.episodes(split) — the difficulty-band
    filter (gym.raft stats) selects episodes whose groups actually carry advantage."""
    from datasets import Dataset

    episodes = env.episodes(split)
    if keep_idx is not None:
        episodes = [episodes[i] for i in keep_idx if 0 <= i < len(episodes)]
    if limit and limit > 0:
        episodes = episodes[:limit]
    # Instruction-style tasks (e.g. iac) need the model's chat template; a conversational
    # prompt (list of messages) makes TRL apply it. Completion-style tasks (a bare
    # prompt -> text) stay raw. Keep train + eval consistent via env.use_chat_template.
    chat = getattr(env, "use_chat_template", False)
    def _p(ep):
        p = env.prompt(ep)
        return [{"role": "user", "content": p}] if chat else p
    rows = {"prompt": [_p(ep) for ep in episodes],
            "ep_id": list(range(len(episodes)))}
    return Dataset.from_dict(rows), episodes


def build_sft_dataset(env: Environment, split: str, limit: int = 0):
    """(prompt -> oracle) pairs for an SFT warm-start. We have a correct oracle per
    task, so imitating it teaches the output shape (e.g. a terse ```hcl block, not
    verbose prose) + baseline-correct patterns before GRPO refines.

    Chat envs (use_chat_template) get conversational `messages` so TRL applies the
    same chat template used at train/eval; completion-style envs get {prompt,
    completion} for completion-only loss."""
    from datasets import Dataset

    episodes = env.episodes(split)
    if limit and limit > 0:
        episodes = episodes[:limit]
    if getattr(env, "use_chat_template", False):
        rows = {"messages": [
            [{"role": "user", "content": env.prompt(ep)},
             {"role": "assistant", "content": env.oracle(ep)}]
            for ep in episodes]}
    else:
        rows = {"prompt": [env.prompt(ep) for ep in episodes],
                "completion": [env.oracle(ep) for ep in episodes]}
    return Dataset.from_dict(rows)


def make_reward_fn(env: Environment, episodes):
    """TRL reward fn: `reward_fn(prompts, completions, ep_id, **kw) -> list[float]`.
    Reconstructs each episode by index and scores the completion via the env."""
    def reward_fn(prompts, completions, ep_id, **kwargs):
        out = []
        for completion, i in zip(completions, ep_id):
            text = completion if isinstance(completion, str) else completion[0]["content"]
            out.append(score_completion(env, text, episodes[int(i)]).reward)
        return out
    return reward_fn
