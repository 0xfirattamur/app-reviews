"""Resumable cursors: a provider's raw cursor, tied to the walk that issued it.

A raw cursor only means something to the provider, app, country and client
settings that produced it. Handed back under different ones it silently returns
the wrong pages. A cursor given to a caller therefore carries a short
fingerprint of that scope, and resuming under another scope is refused before a
request is sent.

The wrapped form is ``ar1~<fingerprint>~<raw>``. Callers treat it as opaque.
Providers only ever see raw cursors, so cycle and stall detection in the walk
compare raw cursors too.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

_SEPARATOR = "~"
_PREFIX = f"ar1{_SEPARATOR}"
_FINGERPRINT_LENGTH = 8


@dataclass(frozen=True)
class CursorScope:
    """Everything that decides what a raw cursor refers to.

    ``options`` holds the client's cursor-shaping settings, already rendered as
    strings, such as the server-side filters of an App Store Connect client.
    """

    source: str
    app_id: str
    country: str
    options: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """The first 8 hex characters of a SHA-256 over a stable encoding."""
        encoded = json.dumps(
            [self.source, self.app_id, self.country, list(self.options)],
            separators=(",", ":"),
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return digest[:_FINGERPRINT_LENGTH]


def wrap_cursor(raw: str, scope: CursorScope) -> str:
    """The caller-facing cursor for ``raw`` issued under ``scope``."""
    return f"{_PREFIX}{scope.fingerprint()}{_SEPARATOR}{raw}"


def unwrap_cursor(cursor: str, scope: CursorScope) -> str:
    """The raw cursor inside ``cursor``, checked against ``scope``.

    A cursor without the ``ar1~`` prefix predates fingerprints (saved from 1.3)
    and is returned unchanged. A wrapped cursor from another scope raises
    ``ValueError`` without echoing the cursor.
    """
    if not cursor.startswith(_PREFIX):
        return cursor
    fingerprint_and_raw = cursor.removeprefix(_PREFIX)
    fingerprint, separator, raw = fingerprint_and_raw.partition(_SEPARATOR)
    if not separator or fingerprint != scope.fingerprint():
        raise ValueError(
            "this cursor was produced for a different app, country or client "
            "settings; resume it with the same ones"
        )
    return raw
