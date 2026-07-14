"""Big-model rule authoring for the IaC verifier — draft → sandbox-compile → VALIDATE.

The verifier is the moat; a big model helps *grow* it (never *be* it). Given a rule
intent plus pass/fail example configs, a big open model (via Token Factory) drafts a
scanner predicate + fix hint. The output is then made trustworthy by two gates a human
review sits behind:

  1. AST all-list compile — the drafted function may use only `re`, str ops, and a few
     safe builtins. No imports, no open/exec/eval/dunders. Model-written code never runs
     with real capabilities.
  2. Executable validation — the compiled predicate must classify EVERY example correctly
     (pass_examples → pass/na, fail_examples → fail). A rule that fails its own tests is
     rejected. This is why a weaker/open model is safe here: the tests catch its mistakes.

A validated rule is (id, severity, fn) + hint, ready to append to rl_gym.iac.scan behind
human sign-off. This is the same RLVR discipline one level up: the artifact is verifiable,
so the generator is fungible.

    # draft with a Token Factory big model, then validate:
    python -m rl_gym.gym.rulegen --spec rl_gym/iac/data/rulespecs/rds_deletion_protection.json \
        --model deepseek-ai/DeepSeek-V4-Pro
    # validate a pre-drafted candidate (no API needed):
    python -m rl_gym.gym.rulegen --spec <spec.json> --candidate <fn.py>
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys

# --- sandbox: what a drafted predicate may reference ---------------------------------
_ALLOWED_CALLS = {"search", "match", "findall", "finditer", "sub", "compile",  # re.*
                  "lower", "upper", "strip", "count", "split", "startswith", "endswith",
                  "any", "all", "len", "bool"}
_ALLOWED_NAMES = {"re", "hcl", "any", "all", "len", "bool", "True", "False", "None"}


class _Safe(ast.NodeVisitor):
    def __init__(self):
        self.err = None

    def _bad(self, node, why):
        if self.err is None:
            self.err = f"{why} (line {getattr(node, 'lineno', '?')})"

    def visit_Import(self, n): self._bad(n, "import not allowed")
    def visit_ImportFrom(self, n): self._bad(n, "import not allowed")
    def visit_With(self, n): self._bad(n, "with not allowed")

    def visit_Attribute(self, n):
        if isinstance(n.attr, str) and n.attr.startswith("__"):
            self._bad(n, f"dunder attribute {n.attr}")
        self.generic_visit(n)

    def visit_Name(self, n):
        # allow local vars (assigned in the fn) + the allow-list; block obvious escapes
        if n.id in {"__import__", "eval", "exec", "open", "compile", "globals", "locals",
                    "getattr", "setattr", "vars"}:
            self._bad(n, f"name {n.id} not allowed")
        self.generic_visit(n)


def safe_compile(src: str):
    """Compile a drafted `def rule(hcl): ...` under the AST allow-list + a stripped
    namespace. Returns the callable or raises ValueError."""
    src = src.strip()
    # `re` is provided in the namespace; models habitually write a redundant `import re`.
    # Strip only that (harmless) — every OTHER import still trips the AST guard below.
    src = re.sub(r"(?m)^\s*import\s+re\s*(?:as\s+re)?\s*$", "", src)
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e}")
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(tree.body) != 1 or not funcs:
        raise ValueError("expected exactly one top-level function definition")
    fn = funcs[0]
    if [a.arg for a in fn.args.args] != ["hcl"]:
        raise ValueError("function must take exactly one arg named 'hcl'")
    v = _Safe(); v.visit(tree)
    if v.err:
        raise ValueError(f"unsafe construct: {v.err}")
    # single namespace = the function's __globals__, so `re`/helpers resolve at CALL time;
    # __builtins__ stripped so open/import/eval are unreachable even via free lookup.
    ns: dict = {"__builtins__": {}, "re": re, "any": any, "all": all, "len": len,
                "bool": bool, "str": str, "range": range, "enumerate": enumerate}
    exec(compile(tree, "<rule>", "exec"), ns)
    return ns[fn.name]


def validate(fn, spec: dict) -> tuple[bool, list[str]]:
    """Predicate must return pass/na on every pass_example and fail on every fail_example."""
    report = []
    ok = True
    for hcl in spec.get("pass_examples", []):
        try:
            v = fn(hcl)
        except Exception as e:
            v = f"ERROR {type(e).__name__}: {e}"
        good = v in ("pass", "na")
        ok = ok and good
        report.append(f"  pass_example -> {v}   {'OK' if good else 'WRONG (want pass/na)'}")
    for hcl in spec.get("fail_examples", []):
        try:
            v = fn(hcl)
        except Exception as e:
            v = f"ERROR {type(e).__name__}: {e}"
        good = v == "fail"
        ok = ok and good
        report.append(f"  fail_example -> {v}   {'OK' if good else 'WRONG (want fail)'}")
    return ok, report


def build_prompt(spec: dict) -> list[dict]:
    ex = lambda tag, xs: "\n".join(f"# {tag} example {i+1}\n{h}" for i, h in enumerate(xs))
    user = f"""You extend a Terraform security scanner. Write ONE Python function:

    def rule(hcl):
        # return "fail" if the config VIOLATES the policy,
        # "pass" if it satisfies it, "na" if the policy does not apply to this config
        ...

Policy ({spec['name']}, severity {spec['severity']}): {spec['intent']}

Constraints: use ONLY the `re` module and string operations. No imports (re is provided),
no file/network/eval. The function must return "na" when the relevant resource is absent.

It MUST classify these correctly:
{ex('PASS (want pass or na)', spec.get('pass_examples', []))}
{ex('FAIL (want fail)', spec.get('fail_examples', []))}

Output ONLY the function in one ```python code block, nothing else."""
    return [{"role": "user", "content": user}]


def extract_fn(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def author(spec: dict, model: str, drafter=None) -> dict:
    """Draft + gate a rule. `drafter(messages)->str` overrides the Token Factory call
    (used to plug in any big model, incl. an in-session one). Returns a result dict."""
    if drafter is None:
        from .llm_client import TokenFactory
        tf = TokenFactory()
        if not tf.available():
            raise SystemExit("TOKEN_FACTORY_API_KEY not set (or pass a drafter)")
        drafter = lambda msgs: tf.chat(model, msgs)
    raw = drafter(build_prompt(spec))
    src = extract_fn(raw)
    try:
        fn = safe_compile(src)
    except ValueError as e:
        return {"accepted": False, "stage": "compile", "error": str(e), "src": src}
    ok, report = validate(fn, spec)
    return {"accepted": ok, "stage": "validate", "report": report, "src": src,
            "rule": {"id": spec["name"], "severity": spec["severity"],
                     "hint": spec.get("hint", "")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="rule spec json (name, severity, intent, pass/fail_examples, hint)")
    ap.add_argument("--model", default="deepseek-ai/DeepSeek-V4-Pro", help="Token Factory model id")
    ap.add_argument("--candidate", default="", help="validate a pre-drafted fn file (skip the API)")
    ap.add_argument("--out", default="", help="write the validated rule source here")
    args = ap.parse_args()

    spec = json.load(open(args.spec))
    if args.candidate:
        drafter = lambda msgs: open(args.candidate).read()
        res = author(spec, args.model, drafter=drafter)
    else:
        res = author(spec, args.model)

    print(f"=== rulegen: {spec['name']} (sev {spec['severity']}) ===")
    print(res.get("src", ""))
    print("-" * 60)
    for line in res.get("report", []):
        print(line)
    if res.get("error"):
        print("REJECTED at", res["stage"], ":", res["error"])
    print("VERDICT:", "ACCEPT (all examples classified correctly)" if res["accepted"]
          else "REJECT — do not merge")
    if res["accepted"] and args.out:
        open(args.out, "w").write(res["src"] + "\n")
        print("wrote", args.out)
    raise SystemExit(0 if res["accepted"] else 2)


if __name__ == "__main__":
    main()
