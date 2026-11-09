"""Logging: human-friendly console output (rich) + rotating JSON-lines file for later analysis."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = (
                    value if isinstance(value, str | int | float | bool | type(None)) else str(value)
                )
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: Path | None = None, level: str = "INFO", console: bool = True) -> None:
    logger = logging.getLogger("leadgen")
    logger.setLevel(level.upper())
    logger.handlers.clear()
    logger.propagate = False

    if console:
        rich = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
        rich.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(rich)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "leadgen.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonLinesFormatter())
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

    # Third-party libraries are noisy at INFO.
    for noisy in ("httpx", "httpcore", "apscheduler", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"leadgen.{name}")
