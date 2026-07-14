"""No-GPU web demo — secure-Terraform generation served over Nebius Token Factory.

Same frontend (demo_page.html) and same /generate contract as webdemo.py, but the model
forward pass runs on Token Factory (serverless, pay-per-token) instead of a local vLLM
engine. The verifier (scanner) is deterministic CPU Python, so the whole thing runs on any
cheap CPU host — or your laptop. **Stdlib http.server only**: no FastAPI/uvicorn/torch/vLLM.

  export TOKEN_FACTORY_API_KEY=...
  python -m rl_gym.iac.webdemo_tf --tuned <tf-tuned-model-id> \
      --big deepseek-ai/DeepSeek-V4-Pro --port 8000
  # open http://localhost:8000

  python -m rl_gym.iac.webdemo_tf --tuned <id> --check    # ping the model id(s), then exit
  python -m rl_gym.iac.webdemo_tf --stub                  # full pipeline, no key/network

The tuned model id is whatever you named your uploaded fine-tune on Token Factory. Until
that upload exists you can point --tuned at any shared Token Factory chat model to exercise
the plumbing (it will simply score lower — it isn't the verifier-tuned one).

NOTE: our production tune is `granite-4.1-8b`, which Token Factory does NOT host as a
fine-tune base (it hosts LoRA on Llama-3 / Qwen bases). Production therefore serves the tune
IN-VPC via `webdemo.py` (self-hosted vLLM); this TF path is for a supported-base tune, or the
stand-in above. The big rule-author model (DeepSeek-V4-Pro) does run on Token Factory.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .env import IacEnv
from . import scan as scan_mod
from .scan import scan, DRIFT_RULES
from .repair import findings_text, build_repair_prompt
from ..gym.core import score_completion
from ..gym.llm_client import TokenFactory
from ..gym import rulegen

RULESPEC_DIR = os.path.join(os.path.dirname(__file__), "data", "rulespecs")

# --stub drafter: a canned CORRECT predicate for rds_deletion_protection, so the
# "grow the verifier" flow is demoable with no key. A real run drafts via the big model.
_STUB_RULE_SRC = r'''```python
def rule(hcl):
    if "aws_db_instance" not in hcl:
        return "na"
    found, idx = False, 0
    while True:
        m = re.search(r'resource\s+"aws_db_instance"', hcl[idx:])
        if not m:
            break
        found = True
        brace = hcl.find("{", idx + m.start())
        depth, i = 0, brace
        while i < len(hcl):
            if hcl[i] == "{":
                depth += 1
            elif hcl[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if not re.search(r'deletion_protection\s*=\s*true', hcl[brace:i]):
            return "fail"
        idx = i + 1
    return "pass" if found else "na"
```'''

# ── canned Terraform for --stub (no key/network): a first pass that misses a hardening
#    rule, and a repair pass that adds it — so the self-repair path is exercised. ──────────
_STUB_GEN = '''resource "aws_s3_bucket" "b" {
  bucket = "customer-invoices"
}
resource "aws_s3_bucket_server_side_encryption_configuration" "b" {
  bucket = aws_s3_bucket.b.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" } }
}
resource "aws_s3_bucket_public_access_block" "b" {
  bucket = aws_s3_bucket.b.id
  block_public_acls = true
  block_public_policy = true
  restrict_public_buckets = true
  ignore_public_acls = true
}'''
_STUB_REPAIR = _STUB_GEN + '''
resource "aws_s3_bucket_versioning" "b" {
  bucket = aws_s3_bucket.b.id
  versioning_configuration { status = "Enabled" }
}'''


class Ctx:
    """Everything the handler needs, filled from CLI args in main()."""
    env: IacEnv
    tf: TokenFactory
    tuned = ""
    big = ""
    tuned_label = "tuned · pass@1 + self-repair"
    repair = True
    max_tokens = 1024
    n_samples = 1
    temperature = 0.7
    tuned_in_p = 0.02
    tuned_out_p = 0.06
    stub = False
    presets: list = []
    lock = threading.Lock()
    page_file = os.path.join(os.path.dirname(__file__), "demo_page.html")


def load_page() -> str:
    with open(Ctx.page_file, encoding="utf-8") as f:
        return f.read()


def score_json(ep, completion: str) -> dict:
    """Scorecard as JSON — identical shape to webdemo.py so the frontend is unchanged."""
    rb = score_completion(Ctx.env, completion, ep)
    hcl = Ctx.env.parse(completion, ep) or ""
    rules = []
    for name, sev, fn in scan_mod.RULES:
        v = fn(hcl)
        if v != "na":
            rules.append({"name": name, "sev": sev, "ok": v == "pass"})
    return {"reward": round(rb.reward, 3), "valid": rb.valid,
            "pass_rate": round(scan(hcl)["pass_rate"], 3) if hcl else 0.0,
            "gates": rb.gates, "rules": rules, "hcl": hcl.strip()[:6000]}


def _gen(prompt: str, temperature: float, is_repair: bool = False) -> tuple[str, int, int]:
    """One completion from the tuned model. Returns (text, in_tokens, out_tokens).
    --stub short-circuits to canned Terraform so the server runs with no key/network."""
    if Ctx.stub:
        text = _STUB_REPAIR if is_repair else _STUB_GEN
        return text, max(1, len(prompt) // 4), max(1, len(text) // 4)
    text, usage = Ctx.tf.chat_usage(Ctx.tuned, [{"role": "user", "content": prompt}],
                                    temperature=temperature, max_tokens=Ctx.max_tokens)
    it = int(usage.get("prompt_tokens") or len(prompt) // 4)
    ot = int(usage.get("completion_tokens") or max(1, len(text) // 4))
    return text, it, ot


def do_generate(body: dict) -> dict:
    req = (body.get("req") or "").strip()[:2000]
    if not req:
        return {"error": "empty request"}
    required = [x for x in (body.get("required") or []) if isinstance(x, str)][:20]
    ep = {"id": "live", "req": req, "required": list(required), "oracle": ""}
    drift_on = bool(body.get("drift"))
    n_over = int(body["n"]) if str(body.get("n") or "").isdigit() else 0
    repair_ab = bool(body.get("repair_ab"))
    prompt = Ctx.env.prompt(ep)
    models: list = []
    with Ctx.lock:
        scan_mod.set_drift_rules(drift_on)          # policy-update: score under drifted rules
        try:
            n_use = max(1, n_over or Ctx.n_samples)
            t0 = time.time()
            in_tok = out_tok = 0
            cards = []
            for i in range(n_use):
                text, it, ot = _gen(prompt, temperature=(0.0 if n_use == 1 else Ctx.temperature))
                in_tok += it
                out_tok += ot
                cards.append(score_json(ep, text))
            best = max(range(len(cards)), key=lambda i: (cards[i]["reward"], cards[i]["pass_rate"]))
            pre = cards[best]
            # self-repair: the model reads its own findings and rewrites the gap
            rcard = None
            rep_findings = None
            if Ctx.repair and pre["reward"] < 1.0 and pre["hcl"]:
                rep_findings = findings_text(pre["hcl"], ep)
                rtext, it, ot = _gen(build_repair_prompt(
                    {**ep, "prev_hcl": pre["hcl"], "findings": rep_findings}),
                    temperature=0.0, is_repair=True)
                in_tok += it
                out_tok += ot
                rcard = score_json(ep, rtext)
            secs = time.time() - t0
            better = rcard and (rcard["reward"], rcard["pass_rate"]) > (pre["reward"], pre["pass_rate"])
            final = rcard if better else pre
            # deterministic verifier-guided patch: a standard KMS key policy is boilerplate the
            # 8B can't be taught without collapsing (six training attempts — see the paper), so
            # the serving layer guarantees it. "The fix belongs in the repair turn, not the weights."
            kms_patched = False
            if Ctx.repair and final.get("hcl") and scan_mod._r_kms_key_policy(final["hcl"]) == "fail":
                pcard = score_json(ep, "```hcl\n" + scan_mod.repair_kms_policy(final["hcl"]) + "\n```")
                if (pcard["reward"], pcard["pass_rate"]) >= (final["reward"], final["pass_rate"]):
                    final, kms_patched = pcard, True
            cost = (in_tok * Ctx.tuned_in_p + out_tok * Ctx.tuned_out_p) / 1e6
            common = {"secs": round(secs, 1), "cost_usd": round(cost, 4),
                      "cost_note": f"{in_tok} in + {out_tok} out tok · Token Factory "
                                   f"${Ctx.tuned_in_p:g}/${Ctx.tuned_out_p:g} per MTok"
                                   + (" · STUB (canned, no API call)" if Ctx.stub else "")}
            if repair_ab and rcard is not None:
                models.append({"name": "① first attempt", "card": pre,
                               "samples": [pre["reward"]], **common})
                models.append({"name": "② after self-repair", "card": final,
                               "samples": [final["reward"]],
                               "repair": {"pass1": pre["reward"], "final": final["reward"],
                                          "improved": better}, **common})
            else:
                m = {"name": Ctx.tuned_label, "card": final,
                     "samples": [c["reward"] for c in cards]}
                if rcard is not None or kms_patched:
                    m["repair"] = {"pass1": pre["reward"], "final": final["reward"],
                                   "improved": better or kms_patched, "kms_patched": kms_patched}
                m.update(common)
                models.append(m)
        finally:
            scan_mod.set_drift_rules(False)
    return {"models": models}


def _load_specs() -> dict:
    """name -> spec dict, from data/rulespecs/*.json."""
    specs = {}
    for p in sorted(glob.glob(os.path.join(RULESPEC_DIR, "*.json"))):
        try:
            s = json.load(open(p))
            specs[s["name"]] = s
        except Exception:
            pass
    return specs


def list_rulespecs() -> list:
    live = {nm for nm, _s, _f in scan_mod.RULES}
    return [{"name": s["name"], "severity": s.get("severity", "medium"),
             "intent": s.get("intent", ""), "live": s["name"] in live}
            for s in _load_specs().values()]


def do_authorize(body: dict) -> dict:
    """Big model DRAFTS a scanner rule from a spec → AST sandbox + executable gates →
    if it passes, apply it LIVE so subsequent generations are judged under it. The
    generator is fungible because the artifact is verified; --stub uses a canned draft."""
    name = (body.get("spec") or body.get("spec_name") or "").strip()
    spec = _load_specs().get(name)
    if not spec:
        return {"accepted": False, "error": f"unknown spec {name!r}", "stage": "spec"}
    if Ctx.stub:
        drafter = lambda msgs: _STUB_RULE_SRC
        model = "STUB (canned draft)"
    else:
        if not Ctx.tf.available():
            return {"accepted": False, "error": "TOKEN_FACTORY_API_KEY not set", "stage": "draft"}
        drafter = lambda msgs: Ctx.tf.chat(Ctx.big, msgs)
        model = Ctx.big
    try:
        res = rulegen.author(spec, Ctx.big, drafter=drafter)
    except Exception as e:
        return {"accepted": False, "error": f"{type(e).__name__}: {e}", "stage": "draft"}
    applied = False
    if res.get("accepted"):
        try:
            fn = rulegen.safe_compile(res["src"])
            with Ctx.lock:
                scan_mod.RULES = [r for r in scan_mod.RULES if r[0] != spec["name"]] \
                    + [(spec["name"], spec["severity"], fn)]
                scan_mod.FIX_HINTS[spec["name"]] = spec.get("hint", "")
            applied = True
        except Exception as e:
            res["accepted"] = False
            res["error"] = f"apply failed: {type(e).__name__}: {e}"
    n_ex = len(spec.get("pass_examples", [])) + len(spec.get("fail_examples", []))
    return {"accepted": bool(res.get("accepted")), "stage": res.get("stage"),
            "report": res.get("report", []), "src": res.get("src", ""),
            "error": res.get("error"), "applied": applied, "n_examples": n_ex,
            "model": model, "rule": {"name": spec["name"], "severity": spec["severity"],
                                     "intent": spec.get("intent", "")}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def log_message(self, *a):          # quiet: no per-request stderr spam
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, load_page(), "text/html; charset=utf-8")
        if self.path == "/config":
            return self._json({"drift_available": Ctx.repair,
                               "drift_rules": [r[0] for r in DRIFT_RULES],
                               "repair": bool(Ctx.repair)})
        if self.path == "/presets":
            return self._json({"presets": Ctx.presets})
        if self.path == "/rulespecs":
            return self._json({"specs": list_rulespecs(),
                               "model": ("STUB" if Ctx.stub else Ctx.big)})
        if self.path == "/healthz":
            return self._json({"ok": True, "backend": "token-factory",
                               "tuned": Ctx.tuned, "big": Ctx.big, "stub": Ctx.stub})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/generate":
            try:
                return self._json(do_generate(body))
            except Exception as e:
                return self._json({"models": [{
                    "name": "tuned · error", "secs": 0, "samples": [], "cost_usd": 0.0,
                    "cost_note": "no charge",
                    "card": {"reward": 0.0, "valid": False, "pass_rate": 0.0, "gates": {},
                             "rules": [], "hcl": f"backend error: {type(e).__name__}: {e}"}}]})
        if self.path == "/authorize-rule":
            try:
                return self._json(do_authorize(body))
            except Exception as e:
                return self._json({"accepted": False, "applied": False,
                                   "error": f"{type(e).__name__}: {e}", "stage": "server"})
        if self.path == "/reload":          # page is re-read per request anyway
            return self._json({"ok": True, "note": "demo_page.html is re-read on every GET /"})
        return self._json({"error": "not found"}, 404)


def check(model: str) -> bool:
    """Ping a Token Factory model id with a 1-token request; report ok/latency."""
    tf = TokenFactory()
    if not tf.available():
        print("  ✗ TOKEN_FACTORY_API_KEY not set")
        return False
    t0 = time.time()
    try:
        txt, usage = tf.chat_usage(model, [{"role": "user", "content": "reply with: ok"}],
                                   temperature=0.0, max_tokens=8)
        print(f"  ✓ {model}  → {time.time()-t0:.2f}s  ({(txt or '').strip()[:40]!r}, "
              f"usage={usage or 'n/a'})")
        return True
    except Exception as e:
        print(f"  ✗ {model}  → {type(e).__name__}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tuned", default=os.environ.get("TF_TUNED_MODEL", ""),
                    help="Token Factory model id for the tuned 8B (your uploaded fine-tune, or "
                    "any shared chat model to test plumbing). Env: TF_TUNED_MODEL")
    ap.add_argument("--big", default=os.environ.get(
                    "TF_BIG_MODEL", "deepseek-ai/DeepSeek-V4-Pro"),
                    help="Token Factory model id for live rule authoring. Env: TF_BIG_MODEL")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--max_tokens", type=int, default=1024)
    ap.add_argument("--n_samples", type=int, default=1,
                    help="tuned best-of-n (1 = pass@1 + self-repair, the serving default)")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--repair", type=int, default=1)
    ap.add_argument("--tuned_prices", default="0.02,0.06",
                    help="$/MTok in,out for the tuned model cost column (Token Factory list)")
    ap.add_argument("--check", action="store_true", help="ping the model id(s) and exit")
    ap.add_argument("--stub", action="store_true",
                    help="serve canned generations (no key/network). Env: STUB=1")
    args = ap.parse_args()
    if os.environ.get("STUB"):
        args.stub = True

    Ctx.env = IacEnv(data_dir="real")
    Ctx.tf = TokenFactory()
    Ctx.tuned = args.tuned
    Ctx.big = args.big
    Ctx.repair = bool(args.repair)
    Ctx.max_tokens = args.max_tokens
    Ctx.n_samples = max(1, args.n_samples)
    Ctx.temperature = args.temperature
    Ctx.stub = bool(args.stub)
    Ctx.tuned_in_p, Ctx.tuned_out_p = (float(x) for x in args.tuned_prices.split(","))
    rep = " + self-repair" if Ctx.repair else ""
    Ctx.tuned_label = (f"tuned · best-of-{Ctx.n_samples}{rep}" if Ctx.n_samples > 1
                       else f"tuned · pass@1{rep}")
    # showcase presets = first 10 real dev episodes (chips 3/7/8 are curated in the page)
    Ctx.presets = [{"n": i, "req": ep["req"], "required": list(ep.get("required") or [])}
                   for i, ep in enumerate(Ctx.env.episodes("dev")[:10])]

    if args.check:
        print("Token Factory model check:")
        ok = True
        for m in filter(None, [args.tuned, args.big]):
            ok = check(m) and ok
        raise SystemExit(0 if ok else 2)

    if not Ctx.stub and not Ctx.tf.available():
        print("[webdemo_tf] WARNING: TOKEN_FACTORY_API_KEY not set — /generate will error. "
              "Use --stub to test the pipeline without a key.")
    if not Ctx.stub and not Ctx.tuned:
        print("[webdemo_tf] WARNING: no --tuned model id — /generate will error.")

    mode = "STUB (canned)" if Ctx.stub else f"Token Factory · tuned={Ctx.tuned or '(unset)'}"
    print(f"[webdemo_tf] backend: {mode}")
    print(f"[webdemo_tf] serving http://localhost:{args.port}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
