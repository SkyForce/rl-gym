"""Checkpoint-save hygiene shared by the SFT and GRPO trainers.

v10 lesson: a checkpoint dir that saves weights but a broken/missing fast tokenizer
(`tokenizer.json`) poisons everything downstream — GRPO-from-SFT inherits it, and
every eval row crashes with "Couldn't instantiate the backend tokenizer". Verify the
tokenizer round-trips at save time and repair it from the source model dir if not.
"""
from __future__ import annotations

import os
import shutil

_TOK_FILES = ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
              "special_tokens_map.json", "chat_template.jinja", "added_tokens.json")


def ensure_tokenizer(output_dir: str, source: str) -> None:
    """Make sure `output_dir` holds a loadable tokenizer; copy files from `source`
    (a local model dir or HF id) if it doesn't. Hard-fails if still broken —
    better to die at save than to ship a checkpoint eval can't load."""
    from transformers import AutoTokenizer

    def loads() -> bool:
        try:
            AutoTokenizer.from_pretrained(output_dir)
            return True
        except Exception:
            return False

    if loads():
        return
    print(f"[gym.modelio] tokenizer in {output_dir} not loadable — repairing from {source}")
    if os.path.isdir(source):
        for name in _TOK_FILES:
            p = os.path.join(source, name)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(output_dir, name))
    else:   # HF id — fetch and save directly
        AutoTokenizer.from_pretrained(source).save_pretrained(output_dir)
    if not loads():
        raise SystemExit(f"[gym.modelio] tokenizer in {output_dir} STILL not loadable after "
                         f"repair from {source} — refusing to ship a broken checkpoint")
    print(f"[gym.modelio] tokenizer repaired OK")
