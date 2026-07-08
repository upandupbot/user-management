# Runs the swap-assignee web UI. Used by Fly.io; Render can use it too
# (or the native Python runtime in render.yaml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY swap_assignee/ swap_assignee/

# Non-root user; audit JSONL falls back to its home dir.
RUN useradd --create-home appuser
USER appuser

ENV PORT=8080
EXPOSE 8080

# Single worker: preview plans and the log ring live in process memory.
CMD gunicorn --workers 1 --threads 8 --timeout 600 --bind 0.0.0.0:$PORT 'swap_assignee.web:create_app()'
