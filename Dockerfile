# Smart Meeting Assistant — Dockerfile
# ─────────────────────────────────────
# Build:   docker build -t meeting-assistant .
# Run:     docker run -p 5000:5000 --env-file .env meeting-assistant
# Dev run: docker run -p 5000:5000 -e FLASK_ENV=development --env-file .env meeting-assistant

FROM python:3.11-slim AS base

# Reproducible builds; no .pyc clutter; unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    PORT=5000

WORKDIR /app

# ── System deps (minimal) ─────────────────────────────────────────────────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps (layer-cached separately from source) ─────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn==22.0.0

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Security: run as non-root ─────────────────────────────────────────────────
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE $PORT

# ── Health check via the /health endpoint ─────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:$PORT/health || exit 1

# ── Production: gunicorn with 2 workers + gthread worker class ────────────────
# Switch to `flask run` for local dev by overriding CMD or setting FLASK_ENV=development.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "2", \
     "--worker-class", "gthread", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
