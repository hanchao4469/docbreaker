"""Run one API process sharing a bounded document/model worker pool."""

import logging
import os

import uvicorn

from .api import create_app

logging.basicConfig(level=logging.INFO, format="%(message)s")
os.umask(0o077)
uvicorn.run(create_app(), host="0.0.0.0", port=8000, workers=1, access_log=False,
            timeout_graceful_shutdown=30, limit_concurrency=256, timeout_keep_alive=30)
