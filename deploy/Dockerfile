# Rectifi web app image. Build context is the repo root (compose does this for you):
#   docker compose --env-file .env.deploy -f deploy/compose.oracle.yml up -d --build
# No secret is baked in: the password hash, session secret and database password arrive at run time (see .env.deploy).
FROM python:3.13-slim-bookworm

# RECTIFI_BIND_HOST / RECTIFI_PUBLIC: this image is reachable from other containers, so the app treats itself as public and
# refuses to start without a login (RECTIFI_PASSWORD_HASH and RECTIFI_SESSION_SECRET). Compose publishes the port on
# 127.0.0.1 only; Tailscale on the host is the only way in.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RECTIFI_BIND_HOST=0.0.0.0 \
    RECTIFI_PUBLIC=1

RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin rectifi

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rectifi/ ./rectifi/
COPY web/dist/ ./web/dist/
COPY scripts/ ./scripts/

USER 10001:10001

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8501/api/health',timeout=3)"]

# One worker on purpose: uploads waiting for a column check and the Ask locks live in memory.
# No --proxy-headers: we do not trust X-Forwarded-* from anyone.
CMD ["python", "-m", "uvicorn", "--factory", "rectifi.api.app:create_app", "--host", "0.0.0.0", "--port", "8501", "--workers", "1"]
