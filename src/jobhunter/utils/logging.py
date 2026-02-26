"""Structured JSON logging with log sanitization for sensitive data."""

import json
import logging
import logging.handlers
import os
import re
from pathlib import Path
from typing import Any


# Patterns that indicate sensitive data — values matched are redacted in logs
_SENSITIVE_KEYS = re.compile(
    r"(password|passwd|secret|token|key|fernet|credential|auth|api_key|access_token)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]+"),   # Anthropic API keys
    re.compile(r"ya29\.[A-Za-z0-9\-_]+"),     # Google OAuth tokens
]


def _redact(value: Any) -> Any:
    """Replace sensitive string values with [REDACTED]."""
    if isinstance(value, str):
        for pattern in _SENSITIVE_VALUE_PATTERNS:
            if pattern.search(value):
                return "[REDACTED]"
    return value


def _sanitize(record_dict: dict) -> dict:
    """Recursively sanitize a log record dict."""
    sanitized = {}
    for k, v in record_dict.items():
        if isinstance(k, str) and _SENSITIVE_KEYS.search(k):
            sanitized[k] = "[REDACTED]"
        elif isinstance(v, dict):
            sanitized[k] = _sanitize(v)
        elif isinstance(v, list):
            sanitized[k] = [_redact(item) for item in v]
        else:
            sanitized[k] = _redact(v)
    return sanitized


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_dict["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            log_dict.update(_sanitize(record.extra))

        return json.dumps(log_dict)


def setup_logging(
    level: str = "INFO",
    log_dir: str = "data/logs",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Configure root logger with JSON formatting to file and console."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = JsonFormatter()

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "jobhunter.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric_level)

    # Console handler (plain text for readability)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    console_handler.setLevel(numeric_level)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call setup_logging() once at startup first."""
    return logging.getLogger(name)


class LogExtra:
    """Helper for attaching structured extra fields to log records."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(msg, extra={"extra": kwargs})

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(msg, extra={"extra": kwargs})

    def error(self, msg: str, **kwargs: Any) -> None:
        self._logger.error(msg, extra={"extra": kwargs})

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(msg, extra={"extra": kwargs})
