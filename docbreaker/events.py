"""Structured operational events, intentionally excluding document bodies and secrets."""

import json
import logging
import time

logger = logging.getLogger("docbreaker")


def event(name: str, **fields):
    logger.info(json.dumps({"event": name, "time": round(time.time(), 3), **fields}, ensure_ascii=False))


class ParseError(Exception):
    """A stable public error category, without upstream response bodies or credentials."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
