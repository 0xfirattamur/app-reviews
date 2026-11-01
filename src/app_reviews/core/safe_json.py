"""JSON parsing that reports hostile nesting as a decode error.

A store's body is untrusted. ``json`` parses nested arrays recursively, so a few
thousand ``[`` raise ``RecursionError``, which no parse handler catches because
none expects it. These wrappers turn it into ``json.JSONDecodeError``, a
``ValueError``, so every handler that already covers malformed JSON covers this.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["loads", "raw_decode"]

_DECODER = json.JSONDecoder()


def loads(text: str) -> Any:
    """``json.loads(text)``, with over-deep nesting raised as ``JSONDecodeError``."""
    try:
        return json.loads(text)
    except RecursionError as exc:
        raise _too_deep(text) from exc


def raw_decode(text: str, start: int) -> tuple[Any, int]:
    """``JSONDecoder.raw_decode(text, start)``, with the same nesting guarantee.

    Returns the decoded value and the index just past it.
    """
    try:
        return _DECODER.raw_decode(text, start)
    except RecursionError as exc:
        raise _too_deep(text) from exc


def _too_deep(text: str) -> json.JSONDecodeError:
    return json.JSONDecodeError("nesting too deep", text, 0)
