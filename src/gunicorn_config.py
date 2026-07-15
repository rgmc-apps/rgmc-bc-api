"""gunicorn server configuration."""
import os

threads = 2
workers = 3
timeout = 120
bind = f":{os.environ.get('PORT', '8080')}"
worker_class = "uvicorn.workers.UvicornWorker"
