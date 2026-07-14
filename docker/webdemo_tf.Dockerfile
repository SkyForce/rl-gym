# Minimal CPU image for the Token-Factory-backed secure-Terraform web demo.
# No GPU, no torch/vLLM/FastAPI — the models run on Nebius Token Factory (per-token),
# the verifier (scanner) is deterministic CPU Python, and the server is stdlib http.server.
# Result: a ~150MB image that runs on the smallest instance any host offers.
FROM python:3.12-slim

WORKDIR /app

# The only runtime third-party dep across the served code path is certifi (TLS CA bundle
# for the Token Factory HTTPS call). Everything else is stdlib + the app.
RUN pip install --no-cache-dir certifi

# App package + the committed IaC-Eval data (rl_gym/iac/data/iac_eval.csv) and rule specs.
# .dockerignore keeps the context to just rl_gym/, so this is the whole image payload.
COPY rl_gym/ ./rl_gym/

ENV PYTHONUNBUFFERED=1 \
    PORT=8000
EXPOSE 8000

# All config is env-driven (see main() in webdemo_tf.py):
#   TOKEN_FACTORY_API_KEY  (secret)   TF_TUNED_MODEL  (your uploaded fine-tune id)
#   TF_BIG_MODEL (rule authoring)     STUB=1          (keyless canned demo)
CMD ["python", "-m", "rl_gym.iac.webdemo_tf"]
