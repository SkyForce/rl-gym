"""Re-score the static Claude Fable 5 baseline (docs/fable-baseline/) — no GPU, no API key.

The exhibit: the same 10 IaC-Eval dev episodes the models are evaluated on, answered by
claude-fable-5 (2026-07-06..09, pass@1, one attempt, no post-scoring edits) under two
conditions, scored by the same scanner+gates as every other row in the README:

  blind/     fresh context — only the episode prompt (first-contact frontier model)
  rulebook/  fresh context + docs/fable-baseline/rulebook.txt (~580 tokens) simulating an
             agent's memory of the verifier after a few audit cycles

Usage:  python scripts/score_fable_baseline.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from rl_gym.iac.env import IacEnv
from rl_gym.iac.scan import RULES
from rl_gym.gym.core import score_completion

BASE = pathlib.Path(__file__).resolve().parents[1] / "docs" / "fable-baseline"


def score_dir(env, dev, name: str):
    total, gates_ok, rows = 0.0, 0, []
    for i, ep in enumerate(dev):
        completion = (BASE / name / f"ep{i}.md").read_text()
        rb = score_completion(env, completion, ep)
        ok = all(rb.gates.values()) if rb.gates else False
        hcl = env.parse(completion, ep) or ""
        fails = [n for n, _s, f in RULES if f(hcl) == "fail"]
        total += rb.reward
        gates_ok += ok
        rows.append((i, rb.reward, ok, fails))
    return total / len(dev), gates_ok / len(dev), rows


def main():
    env = IacEnv(data_dir="real")
    dev = env.episodes("dev")[:10]
    print(f"{'ep':<4}{'blind':>8}{'rulebook':>10}   blind failed rules")
    print("-" * 64)
    _, _, blind = score_dir(env, dev, "blind")
    _, _, mem = score_dir(env, dev, "rulebook")
    for (i, br, bg, bf), (_, mr, mg, _mf) in zip(blind, mem):
        note = ", ".join(bf) or "-"
        if not bg:
            note += "  [GATE]"
        print(f"{i:<4}{br:>8.3f}{mr:>10.3f}   {note}")
    print("-" * 64)
    b_mean = sum(r for _, r, _, _ in blind) / 10
    m_mean = sum(r for _, r, _, _ in mem) / 10
    b_gate = sum(g for _, _, g, _ in blind) / 10
    m_gate = sum(g for _, _, g, _ in mem) / 10
    print(f"{'mean':<4}{b_mean:>8.3f}{m_mean:>10.3f}   gates {b_gate:.0%} vs {m_gate:.0%}")
    print("\nreference rows (n=120, same scanner): base .624 | SFT .655 | GRPO .737 | human .459")


if __name__ == "__main__":
    main()
