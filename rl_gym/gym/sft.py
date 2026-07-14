"""SFT warm-start for any gym `Environment` — imitate the oracle to teach format + baseline.

For a verifiable-reward vertical we already have a fully-correct oracle per task, so a
short SFT pass on (prompt -> oracle) teaches the model the exact output shape (e.g. a
*terse* ```hcl block instead of verbose prose that overruns the length cap) and the
baseline-correct patterns the base model misses. Standard SFT->RL bootstrap: run this,
then GRPO with `--model <this output_dir>`.

Runs on CUDA (H100) or Apple Silicon / MPS. LoRA keeps 7B within one GPU.

    python -m rl_gym.gym.sft --env iac --model Qwen/Qwen2.5-7B-Instruct --lora \
        --epochs 2 --max_len 1280 --output_dir ./out/iac-sft
"""
from __future__ import annotations

import argparse
import inspect
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="iac", help="gym Environment to warm-start (registry name)")
    ap.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct",
                    help="HF id, local path, or s3://bucket/prefix (base-weights cache)")
    ap.add_argument("--data_dir", default=None)
    ap.add_argument("--output_dir", default="./out/sft")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--max_len", type=int, default=1280,
                    help="max sequence length; oracles are short but keep headroom for the prompt")
    ap.add_argument("--limit", type=int, default=0, help="cap #train episodes (0=all)")
    ap.add_argument("--lora", action="store_true",
                    help="LoRA (PEFT) — needed to fit 7B+ on a single GPU")
    ap.add_argument("--lora_r", type=int, default=16)
    ap.add_argument("--lora_alpha", type=int, default=32)
    ap.add_argument("--precision", choices=["fp32", "bf16"], default="fp32",
                    help="fp32 default: bf16 SFT NaN-collapsed deterministically (v9/v11) on clean "
                         "data — fp32 removes the whole numerics class; the run is short anyway")
    ap.add_argument("--data_file", default="",
                    help="jsonl of chat `messages` rows (gym.raft output) — imitate the model's "
                         "own verifier-perfect answers on real prompts instead of templated oracles")
    ap.add_argument("--mix_oracle", type=int, default=0,
                    help="with --data_file: also mix in N parametric-oracle rows (format grounding)")
    args = ap.parse_args()

    import torch
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    bf16 = args.precision == "bf16" and device == "cuda" and torch.cuda.is_bf16_supported()
    fp16 = False
    print(f"[gym.sft] env={args.env} | device={device} | precision={'bf16' if bf16 else 'fp32'} "
          f"| model={args.model}")

    from trl import SFTConfig, SFTTrainer
    from .registry import get_env
    from .data import build_sft_dataset
    from rl_gym.gym.s3io import materialize

    model_path = materialize(args.model)   # no-op for HF ids / local paths
    if model_path != args.model:
        print(f"[gym.sft] base model materialized from {args.model} -> {model_path}")

    env = get_env(args.env, data_dir=args.data_dir)
    chat = getattr(env, "use_chat_template", False)
    if args.data_file:
        from datasets import load_dataset, concatenate_datasets
        ds = load_dataset("json", data_files=args.data_file)["train"]
        print(f"[gym.sft] RAFT data: {len(ds)} rows from {args.data_file}")
        if args.mix_oracle > 0:
            # parametric env only: its oracles are verified 1.0 — real-data "oracles" are
            # human references (~0.45 security) and must never be imitation targets
            extra = build_sft_dataset(get_env(args.env, data_dir=None), "train",
                                      limit=args.mix_oracle)
            extra = extra.select_columns([c for c in extra.column_names if c in ds.column_names])
            ds = concatenate_datasets([ds, extra]).shuffle(seed=0)
            print(f"[gym.sft] + {len(extra)} parametric-oracle rows mixed in -> {len(ds)} total")
        chat = True   # raft rows are chat `messages`
    else:
        ds = build_sft_dataset(env, "train", limit=args.limit)

    # batch 2 x accum 8 (was 4x4, same effective batch 16): fp32 activations at full
    # max_len with NO gradient checkpointing OOM'd an 80GB H100 the moment the data
    # got long — RAFT rows are ~1k-token real completions, 3x the parametric oracles
    # this config was sized for. Halving the micro-batch halves peak activations.
    desired = dict(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=(int(os.environ.get("RLGYM_SFT_MICROBATCH", "2")) if device == "cuda" else 2),
        gradient_accumulation_steps=(int(os.environ.get("RLGYM_SFT_ACCUM", "8")) if device == "cuda" else 2),
        learning_rate=args.lr,
        # Stability bundle — SFT has NaN-collapsed before (iac v9
        # hit loss 1.8 -> 69 -> nan mid-epoch): warm up the LR, clip grads explicitly,
        # and avoid gradient_checkpointing (PEFT + reentrant checkpointing is a known
        # source of silent bad grads; LoRA-7B @ 1280 fits an 80GB H100 without it).
        warmup_ratio=0.1,
        max_grad_norm=1.0,
        gradient_checkpointing=False,
        logging_steps=10,
        save_steps=10_000,          # only the final save (short run)
        report_to="none",
        bf16=bf16,
        fp16=fp16,
        max_length=args.max_len,        # newer trl
        max_seq_length=args.max_len,    # older trl
        packing=False,
    )
    # completion-only loss applies to {prompt, completion} data; for conversational
    # `messages` we let TRL apply the chat template + its default masking (avoids the
    # assistant_only_loss template-marker requirement that not all chat templates meet).
    if not chat:
        desired["completion_only_loss"] = True

    accepted = set(inspect.signature(SFTConfig.__init__).parameters)
    dropped = sorted(k for k in desired if k not in accepted)
    if dropped:
        print(f"[gym.sft] SFTConfig in this trl version ignores: {dropped}")
    cfg = SFTConfig(**{k: v for k, v in desired.items() if k in accepted})

    peft_config = None
    if args.lora:
        from peft import LoraConfig
        peft_config = LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.05,
                                 target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
                                 task_type="CAUSAL_LM")
        print(f"[gym.sft] LoRA on (r={args.lora_r}, alpha={args.lora_alpha})")

    tkw = dict(model=model_path, args=cfg, train_dataset=ds)
    if peft_config is not None and "peft_config" in inspect.signature(SFTTrainer.__init__).parameters:
        tkw["peft_config"] = peft_config
    trainer = SFTTrainer(**tkw)
    trainer.train()

    # Merge LoRA into the base so GRPO/eval load a plain model.
    to_save = trainer.model
    if args.lora and hasattr(to_save, "merge_and_unload"):
        to_save = to_save.merge_and_unload()

    # NaN guard — v9 NaN-collapsed mid-SFT and the poisoned merge then crashed GRPO
    # downstream with an inscrutable vLLM error. Fail HERE, loudly, and save nothing;
    # the job script falls back to GRPO-from-base.
    bad = [n for n, p in to_save.named_parameters()
           if not torch.isfinite(p.detach()).all()]
    if bad:
        raise SystemExit(f"[gym.sft] REFUSING TO SAVE: non-finite weights in {len(bad)} tensors "
                         f"(training diverged — e.g. {bad[:3]}); nothing written to {args.output_dir}")

    if to_save is not trainer.model:   # merged LoRA path
        from transformers import AutoTokenizer
        print("[gym.sft] saving merged LoRA model")
        to_save.save_pretrained(args.output_dir)
        AutoTokenizer.from_pretrained(model_path).save_pretrained(args.output_dir)
    else:
        print("[gym.sft] saving via trainer.save_model (no merge path)")
        trainer.save_model(args.output_dir)
    from .modelio import ensure_tokenizer
    ensure_tokenizer(args.output_dir, model_path)   # v10: broken tokenizer poisoned everything downstream
    print(f"Saved SFT '{args.env}' agent to {args.output_dir}: "
          f"{sorted(os.listdir(args.output_dir))}")


if __name__ == "__main__":
    main()
