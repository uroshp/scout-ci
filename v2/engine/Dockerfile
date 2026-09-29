# The Ask Scout engine (WS2 step 4b, 2026-09-28). Build context = v2/ (so `scout/` and `engine/` are
# both in the image and config.APP_ROOT resolves to /app, like the viewer image).
#
#   docker build -f engine/Dockerfile -t scout-engine .     (from v2/)
#
# The Agent SDK bundles the Claude CLI binary (no Node needed); it wants a writable HOME. One
# uvicorn worker: the capture/cost state is process-global, and Cloud Run runs at --concurrency 1.
#
# NOT root: the CLI refuses `--dangerously-skip-permissions` (the SDK's bypassPermissions mode)
# under uid 0 ("cannot be used with root/sudo privileges"), which surfaced on Cloud Run as a
# ProcessError six seconds into every question (2026-09-28). The app runs as `scout`.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements-engine.txt .
RUN pip install --no-cache-dir -r requirements-engine.txt
COPY . .
RUN useradd -m -u 1000 scout && chown -R scout:scout /app
USER scout
ENV HOME=/home/scout PORT=8081
EXPOSE 8081
CMD ["sh", "-c", "uvicorn engine.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75"]
