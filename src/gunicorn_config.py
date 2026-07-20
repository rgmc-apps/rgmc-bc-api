"""gunicorn server configuration."""
import os

# Single worker so all requests share one in-memory cache and one BC semaphore.
# UvicornWorker is async — concurrent requests are handled by FastAPI's thread pool
# (anyio default: 40 threads for sync handlers), not gunicorn's `threads` setting.
# BC concurrency is governed solely by _bc_semaphore(3) in bc_functions.py.
workers = 1
timeout = 180
bind = f":{os.environ.get('PORT', '8080')}"
worker_class = "uvicorn.workers.UvicornWorker"
