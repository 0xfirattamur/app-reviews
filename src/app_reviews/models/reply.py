"""A developer's public reply to one review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app_reviews.models.types import ReplyState


@dataclass(frozen=True, slots=True)
class ReviewReply:
    """The reply a store holds for one review.

    ``reply_id`` is the store's own id for the reply: App Store Connect has one
    (a ``customerReviewResponses`` resource), Google Play does not, so it is None
    there.

    ``state`` is ``"pending"`` while Apple has accepted the reply but not yet
    shown it, which can take up to 24 hours, and ``"published"`` once it is
    visible. Play applies a reply immediately, so its replies are always
    ``"published"``.

    ``updated_at`` is when the reply was last written, timezone-aware, or None
    when the store did not say.
    """

    review_id: str
    reply_id: str | None
    text: str
    state: ReplyState
    updated_at: datetime | None
