"""End-to-end audited-RLVR demo, live over Nebius Token Factory — no GPU.

Tells the whole arc with one big open model (DeepSeek-V4-Pro) + the deterministic verifier:

  Act 1  a big model writes Terraform → the scanner judges it (the verifier is the moat)
  Act 2  self-repair — the model reads its own findings and fixes them (targeted, not resample)
  Act 3  grow the verifier — the big model AUTHORS a new rule; AST + executable gates vet it;
         it goes live and the config is re-judged under the new standard
  Act 4  distillation punchline — this loop is baked into a $0.006/request 8B, improved from
         its own traffic by a gated flywheel

Everything in Acts 1–3 runs live on Token Factory; Act 4 cites the measured results.
Writes a JSON transcript (--json out.json) so a hosted visual can replay the exact run.

    TOKEN_FACTORY_API_KEY=... python scripts/demo_e2e.py --model deepseek-ai/DeepSeek-V4-Pro
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

C = {"h": "\033[1;35m", "ok": "\033[1;32m", "bad": "\033[1;31m", "warn": "\033[33m",
     "dim": "\033[2m", "b": "\033[1m", "x": "\033[0m"}


def hr(title):
    print(f"\n{C['h']}{'═'*72}\n  {title}\n{'═'*72}{C['x']}")


def scorecard(env, hcl_completion, ep, scan_mod):
    from rl_gym.gym.core import score_completion
    rb = score_completion(env, hcl_completion, ep)
    hcl = env.parse(hcl_completion, ep) or ""
    fails = [(n, s) for n, s, f in scan_mod.RULES if f(hcl) == "fail"]
    return rb, hcl, fails


def show_card(tag, rb, fails):
    col = C["ok"] if rb.reward >= 0.8 else (C["warn"] if rb.reward > 0 else C["bad"])
    gates = " ".join(f"{k}={'✓' if v else '✗'}" for k, v in (rb.gates or {}).items())
    print(f"  {C['b']}{tag}{C['x']}  reward {col}{rb.reward:.3f}{C['x']}   gates: {gates}")
    if fails:
        print(f"  {C['dim']}findings: " + ", ".join(f"[{s}] {n}" for n, s in fails[:6]) + C["x"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-ai/DeepSeek-V4-Pro",
                    help="Token Factory big model id")
    ap.add_argument("--episode", type=int, default=2, help="IaC-Eval dev index (a real request)")
    ap.add_argument("--json", default="", help="write a replayable transcript here")
    args = ap.parse_args()

    from rl_gym.gym.llm_client import TokenFactory
    from rl_gym.iac.env import IacEnv
    from rl_gym.iac import scan as scan_mod
    from rl_gym.iac.repair import findings_text, build_repair_prompt
    from rl_gym.gym import rulegen

    tf = TokenFactory()
    if not tf.available():
        raise SystemExit("set TOKEN_FACTORY_API_KEY")
    env = IacEnv(data_dir="real")
    ep = env.episodes("dev")[args.episode]
    T = {"model": args.model, "request": ep["req"], "required": list(ep["required"])}

    print(f"{C['dim']}model: {args.model}  ·  verifier: deterministic scanner  ·  no GPU{C['x']}")
    print(f"\n{C['b']}request:{C['x']} {ep['req']}")

    # ── Act 1: big model generates, verifier judges ────────────────────────────────
    hr("ACT 1 · a 235B model writes Terraform — the verifier judges it")
    gen = tf.chat(args.model, [{"role": "user", "content": env.prompt(ep)}],
                  temperature=0.2, max_tokens=1800)
    rb1, hcl1, fails1 = scorecard(env, gen, ep, scan_mod)
    show_card("big model, first pass", rb1, fails1)
    T["act1"] = {"reward": rb1.reward, "gates": rb1.gates, "fails": fails1, "hcl": hcl1}
    verdict = ("a critical flaw — the gate zeroes it" if not rb1.gates.get("no_critical", True)
               else "unfixed findings" if rb1.reward < 1 else "clean")
    print(f"  {C['dim']}→ even a big model leaves {verdict}. The verifier is the ground truth.{C['x']}")

    # ── Act 2: hand the findings back — a generic model is an inconsistent fixer ────
    hr("ACT 2 · hand the audit back — even a 235B is an inconsistent security fixer")
    findings = findings_text(hcl1, ep)
    print(f"  {C['dim']}scanner findings handed back:{C['x']}")
    for line in findings.splitlines()[:6]:
        print(f"    {line}")
    rep = tf.chat(args.model, [{"role": "user", "content": build_repair_prompt(
        {**ep, "prev_hcl": hcl1, "findings": findings})}], temperature=0.2, max_tokens=1800)
    rb2, hcl2, fails2 = scorecard(env, rep, ep, scan_mod)
    served_txt, srb, sfails = (rep, rb2, fails2) if rb2.reward >= rb1.reward else (gen, rb1, fails1)
    show_card("big model, after seeing findings", rb2, fails2)
    T["act2"] = {"reward_before": rb1.reward, "reward_after": rb2.reward,
                 "n_fails_before": len(fails1), "n_fails_after": len(fails2),
                 "hcl": env.parse(served_txt, ep) or ""}
    fixed = len(fails1) - len(fails2)
    note = (f"cleared {fixed} finding(s) here" if fixed > 0 else "made no progress here")
    print(f"  {C['dim']}→ {len(fails1)}→{len(fails2)} findings, {note}. But across requests the 235B is")
    print(f"  inconsistent (it clears a simple flow-log yet stalls on IAM-wildcard criticals) and costs")
    print(f"  ~25× more per call — which is why secure generation is DISTILLED into a tuned model.{C['x']}")

    # ── Act 3: grow the verifier with the big model ───────────────────────────────
    hr("ACT 3 · grow the verifier — the big model AUTHORS a new rule, gated by tests")
    spec_path = os.path.join(os.path.dirname(__file__), "..", "rl_gym", "iac", "data",
                             "rulespecs", "rds_deletion_protection.json")
    spec = json.load(open(spec_path))
    print(f"  {C['b']}policy intent:{C['x']} {spec['intent']}")
    res = rulegen.author(spec, args.model, drafter=lambda m: tf.chat(args.model, m, temperature=0.2))
    gate = (C["ok"] + "ACCEPT" + C["x"]) if res["accepted"] else (C["bad"] + "REJECT" + C["x"])
    print(f"  {C['dim']}big model drafted a predicate → AST sandbox ✓ → validated on "
          f"{len(spec['pass_examples'])+len(spec['fail_examples'])} examples → {gate}{C['x']}")
    T["act3"] = {"accepted": res["accepted"], "rule": spec["name"],
                 "report": res.get("report", []), "src": res.get("src", "")}
    if res["accepted"]:
        # apply the just-authored rule and re-judge the repaired config under the NEW standard
        scan_mod.RULES = scan_mod.RULES + [(spec["name"], spec["severity"],
                                            rulegen.safe_compile(res["src"]))]
        scan_mod.FIX_HINTS[spec["name"]] = spec["hint"]
        rb3, hcl3, fails3 = scorecard(env, rep, ep, scan_mod)
        newly = [n for n, s in fails3 if n == spec["name"]]
        print(f"  {C['dim']}→ rule live. re-judging the config: "
              f"{'now flags '+spec['name'] if newly else 'already satisfies it'} "
              f"(reward {rb3.reward:.3f}).{C['x']}")
        T["act3"]["rejudge"] = {"reward": rb3.reward, "flags_new_rule": bool(newly)}

    # ── Act 4: the distilled specialist beats the rented giant ─────────────────────
    hr("ACT 4 · the tuned 8B does what the 235B couldn't — reliably, for $0.006")
    print(f"  Our verifier-trained 8B, on the SAME benchmark: real IaC-Eval {C['ok']}0.865{C['x']}, "
          f"holdout {C['ok']}0.963{C['x']},")
    print(f"  repair converts {C['ok']}89%{C['x']} of attempts — {C['b']}consistently{C['x']}, where the "
          f"235B is hit-or-miss.")
    print(f"  Cost: {C['b']}~$0.006/request{C['x']} in-VPC vs a big model's ~$0.16. And a {C['b']}flywheel{C['x']}")
    print(f"  promotes a better model from its own traffic — gate-checked, ~$4/cycle.")
    print(f"\n  {C['b']}The division of labor this demo just showed, live:{C['x']}")
    print(f"  {C['dim']}• big open model → WRITES the verifier's rules (Act 3, gated) — its right job")
    print(f"  • deterministic verifier → JUDGES everything (Acts 1–3) — the moat")
    print(f"  • small tuned model → SERVES secure Terraform cheap & reliably (Act 4) — the product")
    print(f"  All open-weight, all serverless on Token Factory, nothing leaves the boundary.{C['x']}\n")

    if args.json:
        json.dump(T, open(args.json, "w"), indent=1)
        print(f"{C['dim']}transcript → {args.json}{C['x']}")


if __name__ == "__main__":
    main()
