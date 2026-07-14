"""GRPO post-training for any gym `Environment` (verifiable-reward RLVR).

Trains an open model against a registered Environment's composed reward — the
trainer is task-agnostic: `--env iac` (secure
Terraform), etc. The dataset (prompts + ep_id) and the reward function come from
the gym layer (`gym.data`), the reward itself from `env.reward`. No learned reward
model, no logged outcomes — just the calculable, gameable-resistant reward.

Runs on CUDA (Nebius H100 — full speed with vLLM) or Apple Silicon / MPS (small
dev runs; vLLM is auto-disabled, since it is CUDA-only). Install:
    pip install "trl>=0.14" transformers accelerate datasets    # + vllm on CUDA

Run:
    python -m rl_gym.gym.train --env iac --data_dir real
    python -m rl_gym.gym.train --env iac --model Qwen/Qwen2.5-7B-Instruct \
        --max_completion_len 384
"""
from __future__ import annotations

import argparse
import inspect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="iac", help="gym Environment to train (registry name)")
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--data_dir", default=None, help="task data dir (or s3://…); omit for built-in")
    ap.add_argument("--num_generations", type=int, default=8, help="GRPO group size G")
    ap.add_argument("--lr", type=float, default=1e-6)
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--max_prompt_len", type=int, default=1024)
    ap.add_argument("--max_completion_len", type=int, default=48)
    ap.add_argument("--output_dir", default="./out/grpo")
    ap.add_argument("--no_vllm", dest="use_vllm", action="store_false",
                    help="disable vLLM rollouts (fall back to HF generate)")
    ap.add_argument("--vllm_gpu_mem", type=float, default=0.3)
    ap.add_argument("--limit", type=int, default=0, help="cap #train episodes (0=all); for smoke runs")
    ap.add_argument("--max_steps", type=int, default=-1, help="cap optimizer steps (-1=use epochs)")
    ap.add_argument("--beta", type=float, default=0.04, help="KL coefficient (0 disables the KL term)")
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--max_grad_norm", type=float, default=1.0)
    ap.add_argument("--no_scale_rewards", dest="scale_rewards", action="store_false",
                    help="Dr.GRPO: don't divide advantages by std (avoids blow-up at low variance)")
    ap.add_argument("--saturate", type=float, default=1.0,
                    help="cap per-component reward returns (<1.0) to resist single-component "
                         "reward hacking; 1.0 = off")
    ap.add_argument("--lora", action="store_true",
                    help="LoRA (PEFT) — needed to fit 7B+ on a single GPU (full-FT optimizer won't)")
    ap.add_argument("--lora_r", type=int, default=16)
    ap.add_argument("--lora_alpha", type=int, default=32)
    # --- DAPO knobs (modern-trl only; dropped by the signature filter on an older pinned trl) ---
    ap.add_argument("--epsilon_high", type=float, default=None,
                    help="DAPO Clip-Higher: raise the upper PPO clip (e.g. 0.28) to keep exploration "
                         "/ entropy up and resist collapse; leaves the lower clip tight")
    ap.add_argument("--loss_type", default=None,
                    help="trl loss_type: grpo | dr_grpo | dapo | bnpo")
    ap.add_argument("--dynamic_sampling", action="store_true",
                    help="DAPO dynamic sampling: skip groups with zero reward std (all-pass/all-fail "
                         "give no advantage) so training spends compute only on informative groups")
    ap.add_argument("--mask_truncated", action="store_true",
                    help="DAPO: don't train on truncated (length-clipped) completions")
    ap.add_argument("--stats_file", default="",
                    help="gym.raft stats.jsonl — enables the difficulty-band episode filter")
    ap.add_argument("--band", default="0.05,0.95",
                    help="with --stats_file: keep episodes whose base-model mean reward is in "
                         "[lo,hi] OR whose group had any variance (0<frac_perfect<1) — GRPO "
                         "steps go only where the group signal is informative")
    ap.set_defaults(use_vllm=True, scale_rewards=True)
    args = ap.parse_args()

    import torch
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    use_vllm = args.use_vllm and device == "cuda"   # vLLM is CUDA-only (no Metal)
    if args.use_vllm and device != "cuda":
        print(f"[gym.train] device={device}: vLLM needs CUDA — falling back to HF generate.")
    bf16 = device == "cuda" and torch.cuda.is_bf16_supported()   # T4 lacks bf16 -> fp16
    fp16 = device == "cuda" and not bf16
    # GRPO requires per_device_train_batch_size % num_generations == 0 → one prompt's
    # worth of generations per micro-step; grad-accum sets prompts/optimizer step.
    batch = args.num_generations
    grad_accum = 4 if device == "cuda" else 2
    print(f"[gym.train] env={args.env} | device={device} | vllm={use_vllm} | bf16={bf16} | model={args.model}")

    from trl import GRPOConfig, GRPOTrainer
    from .registry import get_env
    from .data import build_dataset, make_reward_fn
    from rl_gym.gym.s3io import materialize

    # --model may be an HF id, a local path, or s3://bucket/prefix (base-weights cache
    # in S3 → no repeat HF download). materialize() is a no-op for the first two.
    model_path = materialize(args.model)
    if model_path != args.model:
        print(f"[gym.train] base model materialized from {args.model} -> {model_path}")

    env = get_env(args.env, data_dir=args.data_dir, saturate=args.saturate)
    keep_idx = None
    if args.stats_file:
        import json
        lo, hi = (float(x) for x in args.band.split(","))
        stats = [json.loads(l) for l in open(args.stats_file) if l.strip()]
        keep_idx = [s["idx"] for s in stats
                    if lo <= s["mean"] <= hi or 0.0 < s.get("frac_perfect", 0.0) < 1.0]
        print(f"[gym.train] difficulty band [{lo},{hi}]: keeping {len(keep_idx)}/{len(stats)} "
              f"episodes (rest are all-fail/all-pass for the base — no group advantage)")
    train_ds, train_eps = build_dataset(env, "train", limit=args.limit, keep_idx=keep_idx)

    # vLLM keys exist only in newer TRL; filter to what this GRPOConfig accepts.
    desired = dict(
        output_dir=args.output_dir,
        learning_rate=args.lr,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=batch,
        gradient_accumulation_steps=grad_accum,
        num_generations=args.num_generations,
        max_prompt_length=args.max_prompt_len,
        max_completion_length=args.max_completion_len,
        temperature=args.temperature,
        beta=args.beta,
        max_grad_norm=args.max_grad_norm,
        scale_rewards=args.scale_rewards,
        fp16=fp16,
        gradient_checkpointing=(device == "cuda"),   # trade compute for GPU memory
        logging_steps=5,
        save_steps=200,
        report_to="none",          # no wandb/tensorboard prompts
        bf16=bf16,
        use_vllm=use_vllm,
    )
    if use_vllm:
        desired["vllm_mode"] = "colocate"
        desired["vllm_gpu_memory_utilization"] = args.vllm_gpu_mem
        # Cap vLLM's context at what training actually uses — long-context bases
        # (granite-4.1: 131k) otherwise demand the full window's KV cache at init
        # (20GiB) and crash colocate. trl 1.7 spells it vllm_max_model_length (and has
        # no prompt truncation, hence the 2048 floor); older trl used ..._len — set
        # both, the signature filter keeps whichever exists.
        ctx = max(2048, args.max_prompt_len + args.max_completion_len + 64)
        desired["vllm_max_model_length"] = ctx
        desired["vllm_max_model_len"] = ctx
    if args.max_steps and args.max_steps > 0:
        desired["max_steps"] = args.max_steps
    # DAPO knobs — only real on a modern trl; the signature filter below prints any
    # this trl version doesn't support (so the job log tells us what actually took).
    if args.epsilon_high is not None:
        desired["epsilon_high"] = args.epsilon_high
    if args.loss_type:
        desired["loss_type"] = args.loss_type
    if args.mask_truncated:
        desired["mask_truncated_completions"] = True
    if args.dynamic_sampling:
        desired["dynamic_sampling"] = True

    accepted = set(inspect.signature(GRPOConfig.__init__).parameters)
    # Some trl versions only do *server*-mode vLLM (a separate `trl vllm-serve`)
    # and lack the colocate key. Rather than hang waiting for a server, fall back
    # to HF generate so the run just works.
    if use_vllm and "vllm_mode" not in accepted:
        print("[gym.train] this trl has no colocate vLLM (server-mode only) — "
              "disabling vLLM, using HF generate.")
        desired["use_vllm"] = False
        desired.pop("vllm_gpu_memory_utilization", None)
        desired.pop("vllm_mode", None)
    dropped = sorted(k for k in desired if k not in accepted)
    if dropped:
        print(f"[gym.train] GRPOConfig in this trl version ignores: {dropped}")
    config = GRPOConfig(**{k: v for k, v in desired.items() if k in accepted})

    # LoRA (PEFT) keeps 7B+ within a single GPU — only the adapters get grads/optimizer.
    peft_config = None
    if args.lora:
        from peft import LoraConfig
        peft_config = LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=0.05,
                                 target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
                                 task_type="CAUSAL_LM")
        print(f"[gym.train] LoRA on (r={args.lora_r}, alpha={args.lora_alpha})")

    # Collapse watch: reward-std ~0 across consecutive logs means the groups carry no
    # advantage (policy collapse or saturated data) — the leading indicator; the eval
    # audit's uniq% is the lagging one. Non-fatal: log loudly + drop a marker file the
    # promotion gate treats as an automatic FAIL.
    from transformers import TrainerCallback

    class _CollapseWatch(TrainerCallback):
        def __init__(self, out_dir, thresh=0.02, patience=12):
            self.out_dir, self.thresh, self.patience = out_dir, thresh, patience
            self.run, self.fired, self.last_entropy = 0, False, None

        def on_log(self, args_, state, control, logs=None, **kw):
            if not logs:
                return
            ent = next((v for k, v in logs.items() if "entropy" in k), None)
            if ent is not None:
                self.last_entropy = ent
            std = next((v for k, v in logs.items() if k.endswith("reward_std")), None)
            if std is None:
                return
            self.run = self.run + 1 if std < self.thresh else 0
            if self.run >= self.patience and not self.fired:
                self.fired = True
                msg = (f"[gym.train] !!! REWARD-STD COLLAPSE: std<{self.thresh} for "
                       f"{self.run} consecutive logs at step {state.global_step} "
                       f"(entropy={self.last_entropy}) — groups carry no signal")
                print(msg, flush=True)
                try:
                    import os as _os
                    _os.makedirs(self.out_dir, exist_ok=True)
                    with open(_os.path.join(self.out_dir, "COLLAPSE_ALARM"), "w") as f:
                        f.write(msg + "\n")
                except OSError:
                    pass

    tkw = dict(model=model_path, reward_funcs=make_reward_fn(env, train_eps),
               args=config, train_dataset=train_ds,
               callbacks=[_CollapseWatch(args.output_dir)])
    if peft_config is not None and "peft_config" in inspect.signature(GRPOTrainer.__init__).parameters:
        tkw["peft_config"] = peft_config
    trainer = GRPOTrainer(**tkw)
    trainer.train()

    # With LoRA, merge the adapter into the base so eval/serve load a plain model.
    if args.lora and hasattr(trainer.model, "merge_and_unload"):
        from transformers import AutoTokenizer
        trainer.model.merge_and_unload().save_pretrained(args.output_dir)
        AutoTokenizer.from_pretrained(model_path).save_pretrained(args.output_dir)
    else:
        trainer.save_model(args.output_dir)
    from .modelio import ensure_tokenizer
    ensure_tokenizer(args.output_dir, model_path)   # v10: broken tokenizer poisoned everything downstream
    print(f"Saved tuned '{args.env}' agent to {args.output_dir}")


if __name__ == "__main__":
    main()
