"""Mine real-world Terraform from GitHub (terraform-aws-modules, Apache-2.0) into
gym episodes for the IaC env.

The module *implementations* (root/modules main.tf) hold the raw `resource` blocks —
the `examples/` dirs mostly call modules, so they carry no resources to gate on.
Each kept file becomes one episode source: its resource types drive the
`builds_required` gate and a templated request; the file itself is kept as the
real-world reference (like IaC-Eval, it is NOT a security ceiling).

Filters: >=1 resource block, <= --max_chars (must fit the completion budget),
dedup by resource-type signature (module repos repeat the same shape across
wrapper files). Output: rl_gym/iac/data/github_tf.jsonl

Usage:
    python scripts/mine_github_tf.py --src /path/to/cloned/repos
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rl_gym.iac.scan import resource_types  # noqa: E402

SKIP_NAMES = {"variables.tf", "outputs.tf", "versions.tf", "providers.tf"}


def _origin(repo_dir: str) -> str:
    """github org/name from the clone's git config (correct multi-org attribution)."""
    cfg = os.path.join(repo_dir, ".git", "config")
    try:
        for line in open(cfg):
            if "url =" in line and "github.com" in line:
                return line.split("github.com")[-1].strip(":/ \n").removesuffix(".git")
    except OSError:
        pass
    return os.path.basename(repo_dir)


def mine(src: str, max_chars: int, out_path: str) -> None:
    rows, seen = [], set()
    n_files = n_res = 0
    origins = {d: _origin(os.path.join(src, d))
               for d in os.listdir(src) if os.path.isdir(os.path.join(src, d))}
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in {".git", ".terraform"}]
        repo = os.path.relpath(root, src).split(os.sep)[0]
        for name in sorted(files):
            if not name.endswith(".tf") or name in SKIP_NAMES:
                continue
            n_files += 1
            path = os.path.join(root, name)
            try:
                hcl = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            # gate on real aws_* resources — drops example-wrapper files whose only
            # blocks are test helpers (random_pet, local_file, null_resource, ...)
            aws = sorted({t for t in resource_types(hcl) if t.startswith("aws_")})
            if not aws or len(aws) > 5:     # >5 types -> unwieldy prompt, skip
                continue
            n_res += 1
            sig = (repo, tuple(aws))        # one exemplar per repo x type-set
            if sig in seen:
                continue
            seen.add(sig)
            # small files keep the HCL as a displayable reference; big module
            # implementations become training-only episodes (GRPO's reward needs only
            # request + required types — the model writes its own config)
            rows.append({
                "id": f"gh-{len(rows)}",
                "repo": origins.get(repo, repo),
                "path": os.path.relpath(path, src),
                "license": "Apache-2.0",
                "resource_types": aws,
                "hcl": hcl if len(hcl) <= max_chars else None,
            })
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    kept_kb = sum(len(r["hcl"]) for r in rows if r["hcl"]) // 1024
    n_ref = sum(1 for r in rows if r["hcl"])
    print(f"scanned {n_files} .tf files | {n_res} with aws resources | kept {len(rows)} "
          f"unique episodes ({n_ref} with reference, {kept_kb} KB) -> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="dir containing cloned terraform-aws-modules repos")
    ap.add_argument("--max_chars", type=int, default=3500,
                    help="max file size (must fit the ~1024-token completion budget)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..",
                                                  "rl_gym", "iac", "data", "github_tf.jsonl"))
    args = ap.parse_args()
    mine(args.src, args.max_chars, os.path.abspath(args.out))
