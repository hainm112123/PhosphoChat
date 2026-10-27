# PhosphoChat: backend + built frontend in one image.
# No secrets, keys, passwords or model choices are baked in: everything site-specific comes
# from the environment (or <NAME>_FILE secret files) when the container starts.
#
#   docker compose up -d --build        (see docker-compose.yml and README "Run with Docker")

# --------------------------------------------------------------------------- frontend build
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --------------------------------------------------------------------------- runtime
FROM python:3.13-slim AS app

# git: every vault is its own git repo and each job ends in a commit.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir .
COPY backend/scripts ./scripts
COPY seed /app/seed
COPY --from=web /web/dist /app/frontend/dist

RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data/vaults /data/db \
 && chown -R app:app /data /app

# PYTHONPATH: import the app from /app/backend (not the copy pip put in site-packages), so
# REPO_ROOT resolves to /app and the built UI at /app/frontend/dist is served.
ENV PYTHONPATH=/app/backend \
    VAULTS_DIR=/data/vaults \
    DATA_DIR=/data/db \
    PYTHONUNBUFFERED=1 \
    PYDANTIC_AI_NO_BANNER=1

USER app
EXPOSE 8000
VOLUME ["/data/vaults", "/data/db"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/auth/status', timeout=4); sys.exit(0)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
