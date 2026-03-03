"""Helpers for sanitizing untrusted text before terminal display."""

import re
from typing import Any

_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_WHITESPACE_RE = re.compile(r"\s{2,}")


def sanitize_terminal_text(value: Any, max_len: int = 500) -> str:
    """
    Remove ANSI/control characters and flatten multiline input.
    """
    text = "" if value is None else str(value)
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _CONTROL_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if max_len > 0 and len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text

