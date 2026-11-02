"""Reading the protobuf ``Timestamp`` the Google Play Developer API reports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def parse_protobuf_timestamp(value: Any) -> datetime:
    """Read a protobuf ``Timestamp`` (``{seconds, nanos}``) as one instant.

    ``nanos`` is sub-second precision for that same instant, not a second
    timestamp. ``seconds`` deliberately has no default: defaulting it to 0
    dated a review the API sent no timestamp for to 1970-01-01, and
    ``updated_at`` is the only date this source reports, so ``sort``, ``since``
    and ``until`` all read it, so a wrong one is worse than a dropped review.
    """
    seconds = int(value["seconds"])
    nanos = int(value.get("nanos", 0))
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanos // 1000)
