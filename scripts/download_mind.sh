#!/usr/bin/env bash
# Download the MIND (Microsoft News Dataset) small split.
# Run locally (the Azure blob host is not on every sandbox allowlist).
set -euo pipefail
DEST="${1:-./data/MIND}"
mkdir -p "$DEST/train" "$DEST/dev"
BASE="https://mind201910small.blob.core.windows.net/release"
echo "Downloading MINDsmall to $DEST ..."
curl -L "$BASE/MINDsmall_train.zip" -o /tmp/mind_train.zip
curl -L "$BASE/MINDsmall_dev.zip"   -o /tmp/mind_dev.zip
unzip -o /tmp/mind_train.zip -d "$DEST/train"
unzip -o /tmp/mind_dev.zip   -d "$DEST/dev"
echo "Done. Train: $DEST/train/{behaviors.tsv,news.tsv}  Dev: $DEST/dev/..."
