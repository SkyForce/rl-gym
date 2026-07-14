#!/usr/bin/env bash
# One-time: create the two GitHub-token MysteryBox secrets the IaC job needs, in the
# rl-gym project. Run in a terminal authed for the rl-gym Nebius profile. Values come
# from env vars so nothing sensitive is committed or echoed into the job spec.
#
# Make ONE GitHub *classic* PAT with scopes: repo, read:packages
#   (github.com -> Settings -> Developer settings -> Personal access tokens -> classic)
# then:
#   PROJECT_ID=project-xxxx GH_PAT=ghp_xxxx bash scripts/nebius_secrets.sh
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID to the rl-gym Nebius project (project-...)}"
: "${GH_PAT:?set GH_PAT to a GitHub classic PAT with scopes: repo, read:packages}"
GH_USER="${GH_USER:-SkyForce}"
PROFILE="${PROFILE:-nik}"

mb() { nebius mysterybox "$@" --profile "$PROFILE"; }

id_of() {  # read .metadata.id from get-by-name (empty if not found), no error noise
  mb secret get-by-name --parent-id "$PROJECT_ID" --name "$1" --format json 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["id"])' 2>/dev/null || true
}

put_secret() {  # name, payload-json  — idempotent create, then add a version
  local name="$1" payload="$2" sid
  sid=$(id_of "$name")
  if [ -z "$sid" ]; then
    # create loudly (no error swallowing); tolerate the id-parse (some CLI builds return an
    # async Operation from create, so re-read the id via get-by-name below with a short retry).
    mb secret create --parent-id "$PROJECT_ID" --name "$name" >/dev/null
    for _ in 1 2 3; do sid=$(id_of "$name"); [ -n "$sid" ] && break; sleep 2; done
  fi
  [ -n "$sid" ] || { echo "ERROR: could not create/find '$name'. Run this to see why:
    nebius mysterybox secret create --parent-id $PROJECT_ID --name $name --profile $PROFILE"; exit 1; }
  mb secret-version create --secret "$sid" --payload "$payload" >/dev/null
  echo "  ok: $name ($sid)"
}

echo "### rl-gym-ghcr  (private GHCR pull — REGISTRY_USERNAME / REGISTRY_PASSWORD)"
put_secret rl-gym-ghcr "{\"REGISTRY_USERNAME\":\"$GH_USER\",\"REGISTRY_PASSWORD\":\"$GH_PAT\"}"

echo "### rl-gym-gh    (private repo clone — GH_TOKEN)"
put_secret rl-gym-gh "{\"GH_TOKEN\":\"$GH_PAT\"}"

echo "done — now: PROJECT_ID=$PROJECT_ID S3_BUCKET=<bucket> bash scripts/nebius_launch_iac.sh"
