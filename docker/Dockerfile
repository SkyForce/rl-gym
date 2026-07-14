# Prebuilt GPU image for rl-gym Nebius jobs — bakes the slow deps (torch, vLLM, the
# modern trl stack) so a job skips the multi-minute apt+pip on every run. That pip
# install of torch+vLLM is the single biggest fixed overhead; the base-weights S3
# cache handles the ~15GB model download, and this handles the deps.
#
# Deps only — the repo is NOT baked in. The job clones it at runtime, so code changes
# need only a `git push` (no image rebuild); rebuild only when requirements-gpu.txt moves.
#
# Build in CI on an x86 runner (.github/workflows/build-image.yml) — do NOT build on an
# ARM Mac: CUDA images are x86_64, so a local build is slow QEMU emulation and untestable.
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# build-essential: gcc/g++ for vLLM + torch.compile (triton/inductor) at train time.
# git: the job clones the private repo at runtime. python3 on 22.04 is 3.10 (vLLM-ok).
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
       python3 python3-dev python3-pip git build-essential ca-certificates curl \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

# Resolve + freeze the deps into the image. `pip freeze` is kept for reproducibility
# (so we can pin an exact rebuild later if a floating resolution ever regresses).
COPY requirements-gpu.txt /tmp/requirements-gpu.txt
RUN python -m pip install -U pip \
    && python -m pip install -U -r /tmp/requirements-gpu.txt \
    && python -m pip freeze > /opt/rl-gym-freeze.txt \
    && python -c "import torch, vllm, trl, peft, datasets; print('baked:', torch.__version__, vllm.__version__, trl.__version__)"

WORKDIR /workspace
