"""Generic eval + reward-hacking audit over any Environment.

`compare` prints random -> base -> SFT -> GRPO -> oracle (reward / gate / valid).
`audit` prints the per-component reward breakdown + an env-agnostic degeneracy
probe (unique-action rate, gate rate) — a reward-hacking guardrail reusable for
any registered env. Both read the env's `Reward` (its Components + Gates), so no
task-specific code is needed.

CLI:
    python -m rl_gym.gym.eval compare --env iac --data_dir real \
        --base ibm-granite/granite-4.1-8b --sft ./out/iac-sft --grpo s3://.../iac-grpo
    python -m rl_gym.gym.eval audit   --env iac --data_dir real --grpo s3://...
"""
from __future__ import annotations

import argparse
import os
from typing import Callable

from .core import Environment, score_completion
from .registry import get_env

Policy = Callable[[object], str]   # episode -> completion string


# --------------------------- policies ---------------------------
def vllm_policy(env: Environment, model_path: str, episodes, tok_fallback: str = "",
                n: int = 1, temperature: float = 0.7) -> Policy:
    """CUDA fast path: batch-generate ALL episodes in one vLLM call, hand completions
    back in order. n=1 -> greedy (pass@1). n>1 -> best-of-n: sample n (seeded), the
    scanner selects the best per episode (verifier-guided serving). ~6-10x faster than
    per-episode HF generate; the engine is freed before returning so the next fits."""
    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    from rl_gym.gym.s3io import materialize
    from collections import deque

    p = materialize(model_path)
    try:
        tok = AutoTokenizer.from_pretrained(p)
    except Exception as e:
        if not tok_fallback:
            raise
        print(f"[gym.eval] tokenizer at {p} broken ({type(e).__name__}) — falling back")
        tok = AutoTokenizer.from_pretrained(materialize(tok_fallback))
    chat = getattr(env, "use_chat_template", False) and tok.chat_template
    prompts = []
    for ep in episodes:
        pr = env.prompt(ep)
        prompts.append(tok.apply_chat_template([{"role": "user", "content": pr}],
                                               add_generation_prompt=True, tokenize=False)
                       if chat else pr)
    mnt = getattr(env, "max_new_tokens", 24)
    sp = (SamplingParams(temperature=0.0, max_tokens=mnt) if n == 1
          else SamplingParams(temperature=temperature, n=n, max_tokens=mnt, seed=0))
    llm = LLM(model=p, gpu_memory_utilization=0.85, max_model_len=2048, enforce_eager=True)
    outs = llm.generate(prompts, sp)
    if n == 1:
        texts = deque(o.outputs[0].text for o in outs)
    else:   # scanner picks the best of n per episode
        texts = deque(max((c.text for c in o.outputs),
                          key=lambda t: score_completion(env, t, ep).reward)
                      for ep, o in zip(episodes, outs))
    del llm
    import gc, torch
    gc.collect()
    torch.cuda.empty_cache()
    # evaluate_policy/audit_policy walk the same episode list exactly once, in order
    return lambda ep: texts.popleft()


def tf_policy(env: Environment, model: str, max_new_tokens: int = 0) -> Policy:
    """Serving/eval via Nebius Token Factory (serverless, pay-per-token) instead of a
    self-hosted GPU. `model` is a Token Factory model id — a hosted open model, or your
    own uploaded fine-tune (e.g. the promoted fw1). Needs TOKEN_FACTORY_API_KEY. This is
    the $0-idle managed path; self-hosted in-VPC serving stays on vllm_policy."""
    from .llm_client import TokenFactory
    tf = TokenFactory()
    if not tf.available():
        raise SystemExit("tf_policy needs TOKEN_FACTORY_API_KEY (serverless per-token serving)")
    mnt = max_new_tokens or getattr(env, "max_new_tokens", 512)

    def f(ep) -> str:
        return tf.chat(model, [{"role": "user", "content": env.prompt(ep)}],
                       temperature=0.0, max_tokens=mnt)
    return f


def hf_policy(env: Environment, model_path: str, max_new_tokens: int = 0,
              tok_fallback: str = "") -> Policy:
    """Load an HF checkpoint (base/SFT/GRPO) as a completion policy; greedy decode.
    `model_path` may be local, an HF id, or s3://bucket/prefix (via gym.s3io).
    Generation length defaults to the env's `max_new_tokens` (long for IaC configs). `tok_fallback`: tokenizer source to use if the checkpoint's
    own tokenizer is broken (v10: one bad SFT tokenizer save killed every eval row) —
    for tuned checkpoints the base model's tokenizer is always the right one."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from rl_gym.gym.s3io import materialize

    max_new_tokens = max_new_tokens or getattr(env, "max_new_tokens", 24)
    model_path = materialize(model_path)
    device = ("cuda" if torch.cuda.is_available()
              else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
              else "cpu")
    dtype = torch.bfloat16 if device == "cuda" else torch.float32   # bf16 on GPU: ~2x faster, half mem
    if device == "cuda":
        # cuDNN SDPA can fail ("no valid execution plans") for bf16 on some
        # torch/cuDNN combos — fall back to flash/mem-efficient SDPA.
        try:
            torch.backends.cuda.enable_cudnn_sdp(False)
        except Exception:
            pass
    try:
        tok = AutoTokenizer.from_pretrained(model_path)
    except Exception as e:
        if not tok_fallback:
            raise
        print(f"[gym.eval] tokenizer at {model_path} broken ({type(e).__name__}) — "
              f"falling back to {tok_fallback}")
        tok = AutoTokenizer.from_pretrained(materialize(tok_fallback))
    model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=dtype).to(device)
    model.eval()
    pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id

    use_chat = getattr(env, "use_chat_template", False) and tok.chat_template

    def f(ep) -> str:
        p = env.prompt(ep)
        if use_chat:
            p = tok.apply_chat_template([{"role": "user", "content": p}],
                                        add_generation_prompt=True, tokenize=False)
        ids = tok(p, return_tensors="pt").to(device)
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=max_new_tokens,
                                 do_sample=False, pad_token_id=pad)
        return tok.decode(out[0][ids.input_ids.shape[1]:], skip_special_tokens=True)

    return f


# --------------------------- metrics ---------------------------
def evaluate_policy(env: Environment, policy: Policy, episodes) -> dict:
    rewards, valid, gate = [], 0, 0
    for ep in episodes:
        rb = score_completion(env, policy(ep), ep)
        rewards.append(rb.reward)
        valid += int(rb.valid)
        gate += int(rb.valid and (all(rb.gates.values()) if rb.gates else True))
    n = max(1, len(episodes))
    return {"reward": round(sum(rewards) / n, 4),
            "gate_rate": round(gate / n, 4), "valid_rate": round(valid / n, 4), "n": n}


def audit_policy(env: Environment, policy: Policy, episodes) -> dict:
    comp_keys = [c.name for c in env.reward.components]
    sums = {k: 0.0 for k in comp_keys}
    actions, scored = [], 0
    for ep in episodes:
        rb = score_completion(env, policy(ep), ep)
        if not rb.valid:
            continue
        scored += 1
        for k in comp_keys:
            sums[k] += rb.components.get(k, 0.0)
        actions.append(repr(rb.action))
    s = max(1, scored)
    return {**{k: sums[k] / s for k in comp_keys},
            "unique_action_rate": len(set(actions)) / max(1, len(actions)),
            "n_scored": scored, "_keys": comp_keys}


# --------------------------- tables ---------------------------
def _policies(env, base, sft, grpo, sft_label="SFT (warm-start)", grpo_label="GRPO (RLVR)",
              episodes=None):
    # RLGYM_EVAL_VLLM=1 + CUDA + a known episode list -> batched vLLM policies (fast
    # path; one engine at a time). Anything fails -> per-episode HF generate as always.
    use_vllm = False
    if os.environ.get("RLGYM_EVAL_VLLM") and episodes is not None:
        try:
            import torch, vllm  # noqa: F401
            use_vllm = torch.cuda.is_available()
        except Exception:
            use_vllm = False

    def model_policy(path, tok_fallback=""):
        if use_vllm:
            try:
                return vllm_policy(env, path, episodes, tok_fallback=tok_fallback)
            except Exception as e:
                print(f"[gym.eval] vLLM eval failed for {path} ({type(e).__name__}: {e}) "
                      "— falling back to HF generate")
        return hf_policy(env, path, tok_fallback=tok_fallback)

    rows = [("random", lambda ep: env.random(ep))]
    if base:
        rows.append(("base LLM", model_policy(base)))
    if sft and (sft.startswith("s3://") or os.path.isdir(sft)):
        rows.append((sft_label, model_policy(sft, tok_fallback=base)))
    if grpo and (grpo.startswith("s3://") or os.path.isdir(grpo)):
        rows.append((grpo_label, model_policy(grpo, tok_fallback=base)))
    rows.append(("oracle (ceiling)", lambda ep: env.oracle(ep)))
    return rows


def compare(env, episodes, base, sft, grpo, **labels):
    print(f"\n[{env.name}] eval — {len(episodes)} dev episodes\n")
    hdr = f"{'policy':<26}{'reward':>9}{'gate':>9}{'valid':>9}"
    print(hdr); print("-" * len(hdr))
    for name, pol in _policies(env, base, sft, grpo, episodes=episodes, **labels):
        m = evaluate_policy(env, pol, episodes)
        print(f"{name:<26}{m['reward']:>9.3f}{m['gate_rate']*100:>8.1f}%{m['valid_rate']*100:>8.1f}%")
    print()


def audit(env, episodes, base, sft, grpo, **labels):
    keys = [c.name for c in env.reward.components]
    print(f"\n[{env.name}] reward-hacking audit — {len(episodes)} dev episodes\n")
    hdr = f"{'policy':<26}" + "".join(f"{k[:7]:>8}" for k in keys) + f"{'uniq%':>9}"
    print(hdr); print("-" * len(hdr))
    for name, pol in _policies(env, base, sft, grpo, episodes=episodes, **labels):
        a = audit_policy(env, pol, episodes)
        print(f"{name:<26}" + "".join(f"{a[k]:>8.3f}" for k in keys)
              + f"{a['unique_action_rate']*100:>8.1f}%")
    print("\nRead: a healthy policy lifts the component blend toward oracle (not one"
          " column) and keeps uniq% high (no action collapse).\n")


def repair_compare(env, episodes, models, max_prev_chars=2800):
    """Two-pass SYSTEM eval: generate -> scan -> repair imperfect -> verifier serves the
    better of the two. Same model both turns (the served loop). vLLM batch, per model."""
    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    from rl_gym.gym.s3io import materialize
    from ..iac.repair import findings_text, build_repair_prompt
    import gc, torch

    print(f"\n[{env.name}] two-pass repair eval — {len(episodes)} episodes\n")
    hdr = (f"{'model':<26}{'pass1':>8}{'gate1':>8}{'final':>8}{'gateF':>8}{'repaired':>10}")
    print(hdr); print("-" * len(hdr))
    mnt = getattr(env, "max_new_tokens", 1024)
    for label, path in models:
        p = materialize(path)
        tok = AutoTokenizer.from_pretrained(p)
        chat = getattr(env, "use_chat_template", False) and tok.chat_template

        def render(text):
            return (tok.apply_chat_template([{"role": "user", "content": text}],
                                            add_generation_prompt=True, tokenize=False)
                    if chat else text)

        llm = LLM(model=p, gpu_memory_utilization=0.85, max_model_len=4096, enforce_eager=True)
        sp = SamplingParams(temperature=0.0, max_tokens=mnt)
        outs1 = llm.generate([render(env.prompt(ep)) for ep in episodes], sp)
        r1 = [score_completion(env, o.outputs[0].text, ep) for ep, o in zip(episodes, outs1)]

        todo = []   # (idx, repair_prompt)
        for i, (ep, o, rb) in enumerate(zip(episodes, outs1, r1)):
            hcl = env.parse(o.outputs[0].text, ep)
            if rb.reward < 1.0 and hcl and len(hcl) <= max_prev_chars:
                rep = {**ep, "mode": "repair", "prev_hcl": hcl.strip(),
                       "findings": findings_text(hcl, ep)}
                todo.append((i, build_repair_prompt(rep)))
        finals = list(r1)
        if todo:
            outs2 = llm.generate([render(pr) for _, pr in todo], sp)
            for (i, _), o2 in zip(todo, outs2):
                rb2 = score_completion(env, o2.outputs[0].text, episodes[i])
                if (rb2.reward, rb2.components.get("security", 0)) > \
                   (finals[i].reward, finals[i].components.get("security", 0)):
                    finals[i] = rb2   # verifier serves the better pass
        n = len(episodes)
        g = lambda rs: sum(all(r.gates.values()) if r.gates else False for r in rs) / n
        m = lambda rs: sum(r.reward for r in rs) / n
        improved = sum(f.reward > a.reward for f, a in zip(finals, r1))
        print(f"{label:<26}{m(r1):>8.3f}{g(r1)*100:>7.1f}%{m(finals):>8.3f}{g(finals)*100:>7.1f}%"
              f"{improved:>7}/{len(todo)}")
        del llm
        gc.collect()
        torch.cuda.empty_cache()
    print("\nRead: 'final' is the served system (scan -> repair -> verifier picks); "
          "'repaired' = improved/attempted.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["compare", "audit", "repair_compare"])
    ap.add_argument("--env", default="iac")
    ap.add_argument("--data_dir", default=None)
    ap.add_argument("--n_episodes", type=int, default=300)
    ap.add_argument("--base", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--sft", default="")
    ap.add_argument("--grpo", default="")
    ap.add_argument("--split", default="dev",
                    help="episode split to evaluate (e.g. dev | holdout)")
    ap.add_argument("--sft_label", default="SFT (warm-start)")
    ap.add_argument("--grpo_label", default="GRPO (RLVR)")
    ap.add_argument("--models", default="",
                    help="repair_compare: comma list of label=path (e.g. v16r=s3://...,v18=./out)")
    args = ap.parse_args()

    env = get_env(args.env, data_dir=args.data_dir)
    episodes = env.episodes(args.split)[: args.n_episodes]
    if args.cmd == "repair_compare":
        models = [tuple(m.split("=", 1)) for m in args.models.split(",") if "=" in m]
        repair_compare(env, episodes, models)
        return
    (compare if args.cmd == "compare" else audit)(
        env, episodes, args.base, args.sft, args.grpo,
        sft_label=args.sft_label, grpo_label=args.grpo_label)


if __name__ == "__main__":
    main()
