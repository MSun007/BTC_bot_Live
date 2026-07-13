# Dashboard container for Cloud Run (auto-deploy from GitHub).
# Builds ONLY perp_dashboard_app.py — the trading engine (larry_perp_v1.py)
# is deliberately not copied in, so the public-facing dashboard image never
# contains engine code.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY perp_dashboard_app.py .

# Cloud Run injects $PORT (default 8080). A SINGLE gunicorn worker is intentional:
# the dashboard keeps per-process in-memory state (login rate-limiter, data cache),
# which stays coherent with one worker. Threads provide request concurrency.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 --timeout 90 perp_dashboard_app:app
