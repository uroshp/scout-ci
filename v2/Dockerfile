# Agent Scout — Flask viewer for Cloud Run.
# Build context = v2/ (so scout.config.APP_ROOT, = the dir above scout/, resolves to /app and
# /app/battlecards + /app/docs/mockups exist — both are read at runtime).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-server.txt .
RUN pip install -r requirements-server.txt

# App code + the committed showcase battlecards + assets + the mockup CSS source.
# .dockerignore keeps the venv, caches, the Streamlit shell, and tests out of the image.
COPY . .

ENV PORT=8080
EXPOSE 8080

# A low-traffic read-mostly viewer needs little: 2 workers + threads for the I/O-bound GitHub
# calls (selfserve), a generous timeout for the rare slow render. $PORT is set by Cloud Run.
CMD exec gunicorn --bind :$PORT --workers 2 --threads 8 --timeout 60 server:app
