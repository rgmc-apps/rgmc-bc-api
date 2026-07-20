"""gunicorn server configuration."""
import os

# Single worker so all requests share one in-memory cache and one BC semaphore.
# Multiple workers each get their own cache; Worker 1 warming its cache does nothing
# for Worker 2. With 1 worker the cache is always shared and the semaphore(4) correctly
# caps us at 4 concurrent BC connections (below BC's ~5 limit).
workers = 1
threads = 4
timeout = 180
bind = f":{os.environ.get('PORT', '8080')}"
worker_class = "uvicorn.workers.UvicornWorker"
