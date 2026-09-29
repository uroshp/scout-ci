"""The Ask Scout engine service (WS2 step 4b, 2026-09-28): the one place that holds the model key.
FastAPI + uvicorn on Cloud Run (`scout-engine` / `scout-engine-rc`), the Agent SDK inside, one
worker, concurrency 1. See engine/app.py."""
