"""Repair environment — turn 2 of the generate->scan->repair loop, as a trainable task.

trl's GRPO is single-turn, so the two-turn repair loop is trained by reformulation:
a prep stage (gym.repair_prep) rolls turn 1 with the CURRENT checkpoint, scans it,
and every imperfect config becomes a *repair episode* whose prompt embeds the config
plus the scanner's real findings. The reward is unchanged (same scanner, same gates,
scored on the repaired output) — so the trainer, audit, and ratchet work as-is.

At serving time the same model runs both turns: generate -> scan -> (if imperfect)
repair. `gym.eval repair_compare` measures exactly that two-pass system.

Episodes jsonl schema (written by gym.repair_prep):
  {mode: "repair"|"gen", id, req, required, oracle, prev_hcl?, findings?}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from .env import IacEnv
from .scan import scan, RULES, FIX_HINTS


def findings_text(hcl: str, ep) -> str:
    """Human-readable findings block for a scanned config — the observation the
    repair turn conditions on. Each finding carries its ACTIONABLE fix (FIX_HINTS),
    not just the rule id, so the repair pass knows the concrete change to make (a bare
    id like 'ec2_ebs_optimized' isn't something a model reliably acts on). Gate failures
    (missing/unconfigured types) come first, mirroring the reward structure."""
    from .env import _builds_required
    lines = []
    if not _builds_required(ep, hcl):
        missing = [rt for rt in ep["required"] if rt not in hcl]
        lines.append(f"- GATE builds_required FAILED: missing or unconfigured resource "
                     f"types ({', '.join(missing) if missing else 'declared but empty stubs'})")
    for rid, sev in scan(hcl)["findings"]:
        hint = FIX_HINTS.get(rid)
        lines.append(f"- [{sev.upper()}] {rid}: {hint}" if hint else f"- [{sev.upper()}] {rid}")
    return "\n".join(lines) if lines else "- (no findings)"


def build_repair_prompt(ep) -> str:
    return ("You are a senior DevOps engineer. You previously wrote this Terraform for the "
            f"request: {ep['req']}.\n"
            "```hcl\n" + ep["prev_hcl"].strip() + "\n```\n"
            "A security scanner reviewed it and found:\n"
            f"{ep['findings']}\n"
            f"Rewrite the COMPLETE corrected configuration. It must declare these resources: "
            f"{', '.join(ep['required'])}.\n"
            "Fix every finding, keep the rest intact, and follow security best practices. "
            "Output ONLY the HCL in a ```hcl code block."
            + os.environ.get("RLGYM_PROMPT_SUFFIX", ""))


@dataclass
class IacRepairEnv(IacEnv):
    """IacEnv whose train split is the prepped repair/gen mix. Dev/holdout delegate to
    the plain real-data env so eval numbers stay comparable across model versions.
    `data_dir` = directory containing repair_train.jsonl (from gym.repair_prep)."""
    name: str = "iac_repair"

    def episodes(self, split: str) -> list:
        if split != "train":
            real = IacEnv(data_dir="real", saturate=self.saturate)
            return real.episodes(split)
        path = os.path.join(self.data_dir or ".", "repair_train.jsonl")
        eps = [json.loads(l) for l in open(path) if l.strip()]
        n_rep = sum(e.get("mode") == "repair" for e in eps)
        print(f"[iac.repair] train: {len(eps)} episodes ({n_rep} repair / {len(eps)-n_rep} gen) "
              f"from {path}")
        return eps

    def prompt(self, ep) -> str:
        if ep.get("mode") == "repair":
            return build_repair_prompt(ep)
        return super().prompt(ep)
