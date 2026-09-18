# SmartPark KE production image
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5000

WORKDIR /app

# System deps kept minimal: none needed beyond stdlib + wheels.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py db.py algorithms.py wsgi.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY scripts/ ./scripts/

# SQLite fallback (ephemeral unless a volume is mounted); Postgres is picked
# automatically when DATABASE_URL is set.
VOLUME ["/app/data"]
ENV SQLITE_DB_PATH=/app/data/smartpark.db

EXPOSE 5000

# /health/live = liveness, /health/ready = readiness (DB check).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=os.environ.get('PORT','5000'); urllib.request.urlopen(f'http://127.0.0.1:{p}/health/live', timeout=4); sys.exit(0)"

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 4 --timeout 60 --access-logfile - --error-logfile - wsgi:app"]
