"""Live web demo — type an infra request in the browser, watch base vs tuned write
Terraform, scored by the security scanner in real time.

FastAPI + vLLM (both ship in the GPU image). Two engines (base + tuned) loaded
side-by-side on one H100 (~0.35 GPU mem each, eager mode); if the base engine
fails to load, the demo serves tuned-only rather than dying.

Run (inside the GPU job; see WEBDEMO=1 in scripts/nebius_iac.sh):
    python -m rl_gym.iac.webdemo --tuned s3://.../iac-grpo --base s3://.../base/... --port 8000
"""
from __future__ import annotations

import argparse
import os
import threading

from .env import IacEnv
from . import scan as scan_mod          # module ref: RULES is swapped by set_drift_rules
from .scan import scan
from ..gym.core import score_completion


def score_json(env, ep, completion: str) -> dict:
    """Scorecard as JSON — same verdict logic as demo.py, for the browser to render."""
    rb = score_completion(env, completion, ep)
    hcl = env.parse(completion, ep) or ""
    rules = []
    for name, sev, fn in scan_mod.RULES:
        v = fn(hcl)
        if v != "na":
            rules.append({"name": name, "sev": sev, "ok": v == "pass"})
    return {"reward": round(rb.reward, 3), "valid": rb.valid,
            "pass_rate": round(scan(hcl)["pass_rate"], 3) if hcl else 0.0,
            "gates": rb.gates, "rules": rules, "hcl": hcl.strip()[:6000]}


_PAGE_FILE = __file__.replace("webdemo.py", "demo_page.html")


def load_page() -> str:
    """Re-read the UI on every request: a `git pull` inside a running job updates the
    page live (F5 in the browser) — no restart, no 11-minute model reload for UI tweaks."""
    with open(_PAGE_FILE, encoding="utf-8") as f:
        return f.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tuned", required=True)
    ap.add_argument("--base", default="")
    ap.add_argument("--drift", default="",
                    help="continually-updated checkpoint (trained after +5 drift rules); "
                         "enables the 'policy update' demo toggle")
    ap.add_argument("--big", default=os.environ.get("TF_BIG_MODEL", "deepseek-ai/DeepSeek-V4-Pro"),
                    help="Token Factory big model for the live 'grow the verifier' panel "
                         "(rule authoring). Needs TOKEN_FACTORY_API_KEY in env; the panel stays "
                         "hidden without a key, so the no-key demo is unchanged.")
    ap.add_argument("--repair", type=int, default=1,
                    help="1 = tuned/drift columns run the self-repair pass when pass1 < 1.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--gpu_frac", type=float, default=0.35, help="vLLM GPU memory per engine")
    ap.add_argument("--max_tokens", type=int, default=1024)
    ap.add_argument("--n_samples", type=int, default=1,
                    help="TUNED engine sampling. Default 1 = pass@1 + self-repair: measured to "
                         "match best-of-4 + repair at ~40%% of the generation cost (repair is "
                         "targeted selection; blind resampling became redundant). Set >1 to "
                         "re-enable verifier-guided best-of-n as a robustness fallback.")
    ap.add_argument("--base_n", type=int, default=1,
                    help="BASE engine: 1 = greedy top-1 (standard serving — the realistic "
                         "baseline: without a verifier there is nothing to select with)")
    ap.add_argument("--temperature", type=float, default=0.7, help="sampling temp when n > 1")
    ap.add_argument("--api_model", default="claude-fable-5",
                    help="frontier API comparison column (needs ANTHROPIC_API_KEY in env; '' disables)")
    ap.add_argument("--api_effort", default="medium",
                    help="output_config.effort for the API column (Fable 5: thinking always on, billed)")
    ap.add_argument("--api_prices", default="10,50",
                    help="API $/MTok in,out — Fable 5 is 10,50")
    ap.add_argument("--gpu_hourly", type=float, default=float(__import__("os").environ.get("GPU_HOURLY_USD", "1.55")),
                    help="GPU $/hour for local-engine cost attribution (Nebius L40S-AMD all-in, June 2026)")
    # Latency stack (all degrade gracefully if the vLLM build rejects them):
    ap.add_argument("--quant", default=os.environ.get("QUANT", "fp8"),
                    help="weight quantization: fp8 halves weight traffic on the bandwidth-bound "
                         "L40S (Ada has fp8 kernels); '' = bf16")
    ap.add_argument("--spec", default=os.environ.get("SPEC", "1"),
                    help="ngram speculative decoding: HCL is templated, so prompt-lookup drafts "
                         "verify cheaply and losslessly; '' disables")
    ap.add_argument("--eager", default=os.environ.get("EAGER", ""),
                    help="force enforce_eager=True (the old OOM-safe mode); default tries CUDA "
                         "graphs first and falls back")
    args = ap.parse_args()

    import time
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams
    from rl_gym.gym.s3io import materialize

    env = IacEnv()
    engines = []   # (name, LLM, tokenizer)

    def make_llm(path):
        """Fastest engine the hardware accepts: fp8 + CUDA graphs + ngram speculation,
        falling back one knob at a time to the old eager-bf16 config that always fit."""
        kw = dict(model=path, gpu_memory_utilization=args.gpu_frac, max_model_len=2048)
        if args.quant:
            kw["quantization"] = args.quant
        spec = {"method": "ngram", "num_speculative_tokens": 8,
                "prompt_lookup_max": 4, "prompt_lookup_min": 2}
        attempts = ([{"speculative_config": spec}] if args.spec else []) + [{}]
        last = None
        for extra in attempts:
            for eager in ((True,) if args.eager else (False, True)):
                try:
                    llm = LLM(**kw, enforce_eager=eager, **extra)
                    print(f"[webdemo] engine up: {path.rstrip('/').split('/')[-1]} "
                          f"quant={args.quant or 'bf16'} eager={eager} spec={'ngram' if extra else 'off'}")
                    return llm
                except Exception as e:
                    last = e
                    print(f"[webdemo] engine config rejected (eager={eager}, spec={bool(extra)}): "
                          f"{type(e).__name__}: {e}")
        raise last

    n_t, n_b = max(1, args.n_samples), max(1, args.base_n)
    tuned_path = materialize(args.tuned)
    print(f"[webdemo] loading TUNED engine from {tuned_path}")
    rep_tag = " + self-repair" if args.repair else ""
    tuned_label = (f"tuned · verifier best-of-{n_t}{rep_tag}" if n_t > 1
                   else f"tuned · pass@1{rep_tag}")
    engines.append((tuned_label, make_llm(tuned_path), AutoTokenizer.from_pretrained(tuned_path), n_t))
    if args.base:
        try:
            base_path = materialize(args.base)
            print(f"[webdemo] loading BASE engine from {base_path}")
            base_name = base_path.rstrip("/").split("/")[-1].replace("base_", "")
            base_label = (f"base ({base_name}) · standard top-1" if n_b == 1
                          else f"base ({base_name}) · best-of-{n_b}")
            engines.insert(0, (base_label, make_llm(base_path),
                               AutoTokenizer.from_pretrained(base_path), n_b))
        except Exception as e:   # tuned-only beats no demo
            print(f"[webdemo] WARN: base engine failed ({type(e).__name__}: {e}) — serving tuned-only")
    if args.drift:
        try:
            drift_path = materialize(args.drift)
            print(f"[webdemo] loading DRIFT engine from {drift_path}")
            engines.append((f"drift-updated (v18.1) · best-of-{n_t}{rep_tag}",
                            make_llm(drift_path), AutoTokenizer.from_pretrained(drift_path), n_t))
        except Exception as e:
            print(f"[webdemo] WARN: drift engine failed ({type(e).__name__}: {e}) — toggle disabled")

    # Frontier API column: same prompt, same scanner judge — quality AND cost side by side.
    # Fable 5 notes (per API docs): sampling params are rejected (top-1 only), thinking is
    # always on and billed as output, and safety classifiers may refuse security-adjacent
    # prompts — refusals are displayed as refusals, never silently substituted.
    api_client = None
    if args.api_model and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            api_client = anthropic.Anthropic()
            print(f"[webdemo] API column enabled: {args.api_model} (effort={args.api_effort})")
        except Exception as e:
            print(f"[webdemo] WARN: API column disabled ({type(e).__name__}: {e})")
    elif args.api_model:
        print("[webdemo] API column disabled: no ANTHROPIC_API_KEY in env")
    api_in_p, api_out_p = (float(x) for x in args.api_prices.split(","))

    # Recorded frontier column: with no API key, the committed fable-baseline blind
    # answers (docs/fable-baseline, pass@1, 2026-07-06) stand in for the live API on
    # their 10 IaC-Eval episodes — scored live by the same scanner, cost estimated
    # from measured tokens. Never used for free-form requests.
    def _norm(s):
        return " ".join((s or "").split()).lower()
    canned = {}
    if api_client is None:
        try:
            broot = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "fable-baseline", "blind")
            for i, rep in enumerate(IacEnv(data_dir="real").episodes("dev")[:10]):
                fp = os.path.join(broot, f"ep{i}.md")
                if os.path.exists(fp):
                    with open(fp, encoding="utf-8") as f:
                        canned[_norm(rep["req"])] = {"i": i, "ep": rep, "text": f.read()}
            if canned:
                print(f"[webdemo] recorded fable column: {len(canned)} baseline episodes")
        except Exception as e:
            print(f"[webdemo] recorded fable column disabled ({type(e).__name__}: {e})")

    def api_generate(prompt_text):
        kwargs = dict(model=args.api_model, max_tokens=args.max_tokens,
                      messages=[{"role": "user", "content": prompt_text}])
        try:
            return api_client.messages.create(output_config={"effort": args.api_effort}, **kwargs)
        except TypeError:   # older SDK without output_config — run with defaults
            return api_client.messages.create(**kwargs)

    def sp_for(n):
        # n==1 is greedy (fully deterministic — the reproducible showcase path).
        # n>1 keeps a fixed seed so best-of-n is reproducible click-to-click too.
        return (SamplingParams(temperature=0.0, max_tokens=args.max_tokens) if n == 1
                else SamplingParams(temperature=args.temperature, n=n, max_tokens=args.max_tokens, seed=0))
    lock = threading.Lock()   # one GPU request at a time
    app = FastAPI()

    @app.get("/")
    def index():
        return HTMLResponse(load_page())

    @app.get("/presets")
    def presets():
        return JSONResponse({"presets": [
            {"n": c["i"], "req": c["ep"]["req"], "required": list(c["ep"].get("required") or [])}
            for c in sorted(canned.values(), key=lambda c: c["i"])]})

    @app.post("/reload")
    def reload_page():
        """Self-pull: the container updates its own repo, then load_page() serves the
        new demo_page.html on the next request — HTML/JS tweaks go live WITHOUT a
        relaunch or model reload. (Python-module changes still need a restart.)
        The clone embedded its GH token in the remote URL, so `git pull` re-auths itself."""
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            r = subprocess.run(["git", "-C", root, "pull", "--quiet"],
                               capture_output=True, text=True, timeout=45)
            head = subprocess.run(["git", "-C", root, "log", "-1", "--oneline"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
            return JSONResponse({"ok": r.returncode == 0, "head": head,
                                 "msg": (r.stdout + r.stderr).strip()[-300:]})
        except Exception as e:
            return JSONResponse({"ok": False, "err": f"{type(e).__name__}: {e}"})

    @app.get("/config")
    def config():
        from .scan import DRIFT_RULES
        return JSONResponse({
            "drift_available": any("drift-updated" in nm for nm, _l, _t, _n in engines),
            "drift_rules": [r[0] for r in DRIFT_RULES],
            "repair": bool(args.repair)})

    # ---- the flywheel's intake: every served episode is a training candidate ----
    # gen rows capture real request phrasing; repair rows capture (config, findings,
    # outcome) — exactly the schema gym.repair training consumes. Best-effort: logging
    # must never break serving. Uploaded to s3://$S3_BUCKET/$S3_PREFIX/served/<boot-ts>/.
    _served_path, _served_prefix = None, None
    if os.environ.get("S3_BUCKET") and os.environ.get("AWS_ACCESS_KEY_ID"):
        _boot = int(time.time())
        _served_dir = "/tmp/served"
        os.makedirs(_served_dir, exist_ok=True)
        _served_path = os.path.join(_served_dir, f"served-{_boot}.jsonl")
        _served_prefix = f"{os.environ.get('S3_PREFIX', 'rl-gym-iac')}/served/{_boot}"
        print(f"[webdemo] flywheel intake on -> s3://{os.environ['S3_BUCKET']}/{_served_prefix}/")

    def log_served(row: dict):
        if not _served_path:
            return
        try:
            import json as _json
            from rl_gym.gym.s3io import upload_dir
            with open(_served_path, "a") as f:
                f.write(_json.dumps(row) + "\n")
            upload_dir(os.path.dirname(_served_path), _served_prefix)
        except Exception as e:   # never let telemetry hurt serving
            print(f"[webdemo] served-log skipped ({type(e).__name__}: {e})")

    _cache: dict = {}   # (req, required, engine) -> models payload; demo crowds repeat the presets

    @app.post("/generate")
    def generate(body: dict):
        req = (body.get("req") or "").strip()[:2000]
        if not req:
            return JSONResponse({"error": "empty request"})
        # engines: optional subset like ["tuned"] — the page fires one request per column
        # and renders each card as it lands, instead of waiting for the serial sum.
        want = [w for w in (body.get("engines") or []) if isinstance(w, str)]
        ep = {"id": "live", "req": req, "required": list(body.get("required") or [])[:20], "oracle": ""}
        drift_on = bool(body.get("drift")) and any("drift-updated" in nm for nm, _l, _t, _n in engines)
        n_over = int(body["n"]) if str(body.get("n") or "").isdigit() else 0   # showcase: n=1 greedy
        repair_ab = bool(body.get("repair_ab"))   # emit before/after repair as two cards
        ckey = (req, ",".join(ep["required"]), ",".join(sorted(want)), drift_on, n_over, repair_ab)
        if ckey in _cache:
            return JSONResponse({"models": _cache[ckey]})
        prompt = env.prompt(ep)
        from .repair import findings_text, build_repair_prompt
        models = []
        with lock:
            # the "policy update" switch: scoring + findings run under the drifted rule
            # set for this whole request; restored before the lock releases
            scan_mod.set_drift_rules(drift_on)
            try:
                for name, llm, tok, n in engines:
                    if want and not any(name.startswith(w) for w in want):
                        continue
                    n_use = n_over or n
                    text = tok.apply_chat_template([{"role": "user", "content": prompt}],
                                                   add_generation_prompt=True, tokenize=False)
                    t0 = time.time()
                    outs = llm.generate([text], sp_for(n_use))[0].outputs
                    secs = time.time() - t0
                    toks = sum(len(o.token_ids) for o in outs)
                    cards = [score_json(env, ep, o.text) for o in outs]
                    best = max(range(len(cards)),
                               key=lambda i: (cards[i]["reward"], cards[i]["pass_rate"]))
                    pre = cards[best]
                    # self-repair pass: the model reads its own findings and rewrites
                    rcard, rep_findings = None, None
                    if args.repair and "self-repair" in name and pre["reward"] < 1.0 and pre["hcl"]:
                        rep_findings = findings_text(pre["hcl"], ep)
                        rep = {**ep, "prev_hcl": pre["hcl"], "findings": rep_findings}
                        rtext = tok.apply_chat_template(
                            [{"role": "user", "content": build_repair_prompt(rep)}],
                            add_generation_prompt=True, tokenize=False)
                        r_out = llm.generate([rtext], sp_for(1))[0].outputs
                        toks += sum(len(o.token_ids) for o in r_out)
                        rcard = score_json(env, ep, r_out[0].text)
                        secs = time.time() - t0
                    common = {"secs": round(secs, 1),
                              "cost_usd": round(secs * args.gpu_hourly / 3600, 4),
                              "cost_note": "GPU-seconds at ~$%.2f/h · %.0f tok/s"
                                           % (args.gpu_hourly, toks / max(secs, 1e-6))}
                    better = rcard and (rcard["reward"], rcard["pass_rate"]) > (pre["reward"], pre["pass_rate"])
                    final = rcard if better else pre
                    # flywheel intake: real request phrasing (gen) + the repair episode
                    ts_id = f"srv-{int(time.time())}"
                    log_served({"mode": "gen", "id": ts_id, "req": req,
                                "required": ep["required"], "oracle": "",
                                "reward": final["reward"], "drift": drift_on})
                    if rcard is not None:
                        log_served({"mode": "repair", "id": f"{ts_id}-rep", "req": req,
                                    "required": ep["required"], "oracle": "",
                                    "prev_hcl": pre["hcl"], "findings": rep_findings,
                                    "turn1_reward": pre["reward"],
                                    "final_reward": rcard["reward"], "drift": drift_on})
                    if repair_ab and rcard is not None:
                        # explicit before/after — two cards so the fix is a visible comparison
                        models.append({"name": "① first attempt", "card": pre,
                                       "samples": [pre["reward"]], **common})
                        models.append({"name": "② after self-repair", "card": final,
                                       "samples": [final["reward"]],
                                       "repair": {"pass1": pre["reward"], "final": final["reward"],
                                                  "improved": better}, **common})
                    else:
                        m = {"name": name, "card": final, "samples": [c["reward"] for c in cards]}
                        if rcard is not None:
                            m["repair"] = {"pass1": pre["reward"], "final": final["reward"], "improved": better}
                        m.update(common)
                        models.append(m)
            finally:
                scan_mod.set_drift_rules(False)
        # frontier column runs outside the GPU lock: network-bound (live) or instant (recorded)
        if not want or "api" in want:
            if api_client is not None:
                t0 = time.time()
                try:
                    msg = api_generate(prompt)
                    secs = time.time() - t0
                    usage = msg.usage
                    cost = (usage.input_tokens * api_in_p + usage.output_tokens * api_out_p) / 1e6
                    label = f"{args.api_model} · API top-1 · effort {args.api_effort}"
                    if msg.stop_reason == "refusal":
                        card = {"reward": 0.0, "valid": False, "pass_rate": 0.0, "gates": {},
                                "rules": [], "hcl": "(request refused by the API safety classifier — stop_reason=refusal)"}
                    else:
                        out_text = "".join(b.text for b in msg.content if b.type == "text")
                        card = score_json(env, ep, out_text)
                    models.append({"name": label, "secs": round(secs, 1), "card": card,
                                   "samples": [card["reward"]],
                                   "cost_usd": round(cost, 4),
                                   "cost_note": f"{usage.input_tokens} in + {usage.output_tokens} out tokens (thinking billed)"})
                except Exception as e:
                    models.append({"name": f"{args.api_model} · API (error)", "secs": 0,
                                   "card": {"reward": 0.0, "valid": False, "pass_rate": 0.0,
                                            "gates": {}, "rules": [], "hcl": f"API error: {type(e).__name__}: {e}"},
                                   "samples": [], "cost_usd": 0.0, "cost_note": "no charge"})
            elif canned.get(_norm(req)):
                hit = canned[_norm(req)]
                card = score_json(env, ep, hit["text"])   # live scanner verdict, recorded answer
                in_tok, out_tok = len(prompt) / 3.8, len(hit["text"]) / 3.8 + 1000
                models.append({"name": f"{args.api_model} · blind pass@1 (recorded)", "secs": None,
                               "card": card, "samples": [card["reward"]],
                               "cost_usd": round((in_tok * api_in_p + out_tok * api_out_p) / 1e6, 4),
                               "cost_note": f"estimated: ~{in_tok:.0f} in + ~{out_tok:.0f} out tokens (incl "
                                            f"~1000 assumed thinking) at ${api_in_p:.0f}/${api_out_p:.0f} per MTok — "
                                            "answer recorded 2026-07-06, docs/fable-baseline"})
        if models and not any("(error)" in m["name"] for m in models):
            if len(_cache) > 64:
                _cache.pop(next(iter(_cache)))
            _cache[ckey] = models
        return JSONResponse({"models": models})

    # ── grow the verifier: a big open model drafts a scanner rule, gates vet it, it goes
    #    LIVE so subsequent generations are judged under it. This forward pass runs on Token
    #    Factory (DeepSeek-V4-Pro) — the small tuned model above never authors its own judge.
    #    The panel is hidden unless TOKEN_FACTORY_API_KEY is set, so a no-key run is unchanged.
    import json as _json, glob
    from ..gym.llm_client import TokenFactory
    from ..gym import rulegen
    _RULESPEC_DIR = os.path.join(os.path.dirname(__file__), "data", "rulespecs")
    _tf = TokenFactory()
    print(f"[webdemo] grow-the-verifier panel: "
          f"{'ON (' + args.big + ')' if _tf.available() else 'off (no TOKEN_FACTORY_API_KEY)'}")

    def _load_specs() -> dict:
        specs = {}
        for p in sorted(glob.glob(os.path.join(_RULESPEC_DIR, "*.json"))):
            try:
                s = _json.load(open(p))
                specs[s["name"]] = s
            except Exception:
                pass
        return specs

    @app.get("/rulespecs")
    def rulespecs():
        if not _tf.available():                       # no key -> hide the panel entirely
            return JSONResponse({"specs": [], "model": args.big})
        live = {nm for nm, _s, _f in scan_mod.RULES}
        specs = [{"name": s["name"], "severity": s.get("severity", "medium"),
                  "intent": s.get("intent", ""), "live": s["name"] in live,
                  "maps_to": s.get("_maps_to"),               # external Checkov check the gap maps to
                  "gap": (s.get("fail_examples") or [""])[0][:600]}  # a config our scanner passes, Checkov fails
                 for s in _load_specs().values()]
        return JSONResponse({"specs": specs, "model": args.big})

    @app.post("/authorize-rule")
    def authorize_rule(body: dict):
        name = (body.get("spec") or body.get("spec_name") or "").strip()
        spec = _load_specs().get(name)
        if not spec:
            return JSONResponse({"accepted": False, "error": f"unknown spec {name!r}", "stage": "spec"})
        if not _tf.available():
            return JSONResponse({"accepted": False, "error": "TOKEN_FACTORY_API_KEY not set", "stage": "draft"})
        try:                                          # big model drafts the predicate (network)
            res = rulegen.author(spec, args.big,
                                 drafter=lambda msgs: _tf.chat(args.big, msgs, temperature=0.2))
        except Exception as e:
            return JSONResponse({"accepted": False, "error": f"{type(e).__name__}: {e}", "stage": "draft"})
        applied = False
        if res.get("accepted"):
            try:
                fn = rulegen.safe_compile(res["src"])
                with lock:                            # brief mutation under the serving lock
                    scan_mod.RULES = [r for r in scan_mod.RULES if r[0] != spec["name"]] \
                        + [(spec["name"], spec["severity"], fn)]
                    scan_mod.FIX_HINTS[spec["name"]] = spec.get("hint", "")
                    _cache.clear()                    # re-judge cached generations under the new rule
                applied = True
            except Exception as e:
                res["accepted"] = False
                res["error"] = f"apply failed: {type(e).__name__}: {e}"
        n_ex = len(spec.get("pass_examples", [])) + len(spec.get("fail_examples", []))
        return JSONResponse({"accepted": bool(res.get("accepted")), "stage": res.get("stage"),
                             "report": res.get("report", []), "src": res.get("src", ""),
                             "error": res.get("error"), "applied": applied, "n_examples": n_ex,
                             "model": args.big,
                             "rule": {"name": spec["name"], "severity": spec["severity"],
                                      "intent": spec.get("intent", "")}})

    import uvicorn
    print(f"[webdemo] serving on 0.0.0.0:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
