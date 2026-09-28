"""Replying to App Store reviews through App Store Connect.

Endpoints (App Store Connect API, ``customerReviewResponses``):

- ``POST /v1/customerReviewResponses`` creates the reply, or replaces the one
  already there:
  https://developer.apple.com/documentation/appstoreconnectapi/post-v1-customerreviewresponses
- ``GET /v1/customerReviews/{id}/response`` reads it:
  https://developer.apple.com/documentation/appstoreconnectapi/get-v1-customerreviews-_id_-response
- ``DELETE /v1/customerReviewResponses/{id}`` removes it:
  https://developer.apple.com/documentation/appstoreconnectapi/delete-v1-customerreviewresponses-_id_

The key needs a role that may answer reviews, Customer Support or Admin:
https://developer.apple.com/help/app-store-connect/monitor-ratings-and-reviews/respond-to-reviews
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

from app_reviews.appstore.api import ConnectAPIClient
from app_reviews.core.classify import raise_for_http_failure, raise_for_write_failure
from app_reviews.core.http import HttpResponse
from app_reviews.errors import ParseError
from app_reviews.models.reply import ReviewReply
from app_reviews.models.types import ReplyState

_API = "the App Store Connect API"

_STATES: dict[str, ReplyState] = {
    "PUBLISHED": "published",
    "PENDING_PUBLISH": "pending",
}
"""Apple's documented ``CustomerReviewResponseV1.state`` values."""

_JSON_HEADERS = {"Content-Type": "application/json"}


class AppStoreReplies(ConnectAPIClient):
    """Reads, writes and deletes the developer reply to an App Store review.

    ``review_id`` is the Connect ``customerReviews`` id, which is ``Review.id``
    on reviews fetched with ``AppStoreReviews(auth=...)``.

    Writes (``reply``, ``delete_reply``) are sent exactly once, whatever
    ``retry`` says: a reply is public, and a retried write whose first attempt
    did land cannot be recalled. When the outcome of a write is unknown it
    raises ``ReplyOutcomeUnknownError``; read the state with ``get_reply()``
    before trying again. Reads follow the normal retry policy.

    Every method raises ``AuthError`` for a key that is unusable or lacks the
    role, and ``RateLimitError`` (with ``retry_after``) for a 429.
    """

    def reply(self, review_id: str, text: str) -> ReviewReply:
        """Publish ``text`` as the reply to ``review_id``, replacing any existing one.

        Apple usually answers ``state="pending"``: the reply can take up to 24
        hours to appear. Raises ``ReplyRejectedError`` when Apple refuses it and
        ``ReplyOutcomeUnknownError`` when it may or may not have been applied.
        """
        response = self._http.post(
            f"{self.API_BASE}/v1/customerReviewResponses",
            body=self._reply_body(review_id, text),
            headers=self._headers() | _JSON_HEADERS,
            retryable=False,
        )
        return self._written(response, review_id)

    async def areply(self, review_id: str, text: str) -> ReviewReply:
        """Async equivalent of ``reply``."""
        response = await self._http.apost(
            f"{self.API_BASE}/v1/customerReviewResponses",
            body=self._reply_body(review_id, text),
            headers=await self._aheaders() | _JSON_HEADERS,
            retryable=False,
        )
        return self._written(response, review_id)

    def get_reply(self, review_id: str) -> ReviewReply | None:
        """The reply to ``review_id``, or None when it has none.

        Raises ``NotFoundError`` when Apple has no such review.
        """
        response = self._http.get(
            self._response_url(review_id), headers=self._headers()
        )
        return self._read(response, review_id)

    async def aget_reply(self, review_id: str) -> ReviewReply | None:
        """Async equivalent of ``get_reply``."""
        response = await self._http.aget(
            self._response_url(review_id), headers=await self._aheaders()
        )
        return self._read(response, review_id)

    def delete_reply(self, review_id: str) -> bool:
        """Remove the reply to ``review_id``. False when there was none to remove.

        Looks the reply up first, because Apple deletes by the reply's own id, so
        an unknown review raises ``NotFoundError`` as ``get_reply`` does.
        """
        existing = self.get_reply(review_id)
        if existing is None or existing.reply_id is None:
            return False
        response = self._http.delete(
            self._delete_url(existing.reply_id),
            headers=self._headers(),
            retryable=False,
        )
        return self._deleted(response)

    async def adelete_reply(self, review_id: str) -> bool:
        """Async equivalent of ``delete_reply``."""
        existing = await self.aget_reply(review_id)
        if existing is None or existing.reply_id is None:
            return False
        response = await self._http.adelete(
            self._delete_url(existing.reply_id),
            headers=await self._aheaders(),
            retryable=False,
        )
        return self._deleted(response)

    def _reply_body(self, review_id: str, text: str) -> str:
        return json.dumps(
            {
                "data": {
                    "type": "customerReviewResponses",
                    "attributes": {"responseBody": text},
                    "relationships": {
                        "review": {"data": {"type": "customerReviews", "id": review_id}}
                    },
                }
            }
        )

    def _response_url(self, review_id: str) -> str:
        """Escaped: the id lands in the path of a request carrying the JWT."""
        return (
            f"{self.API_BASE}/v1/customerReviews/{quote(review_id, safe='')}/response"
        )

    def _delete_url(self, reply_id: str) -> str:
        return f"{self.API_BASE}/v1/customerReviewResponses/{quote(reply_id, safe='')}"

    def _written(self, response: HttpResponse, review_id: str) -> ReviewReply:
        raise_for_write_failure(response, _API)
        try:
            return self._parse(response.json()["data"], review_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(
                f"{_API} accepted the reply but its answer could not be read: {exc}",
                status=response.status,
            ) from exc

    def _read(self, response: HttpResponse, review_id: str) -> ReviewReply | None:
        """``data: null`` is a review without a reply; a 404 is no such review."""
        raise_for_http_failure(response, _API)
        try:
            data = response.json()["data"]
            return None if data is None else self._parse(data, review_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(
                f"Malformed reply from {_API}: {exc}", status=response.status
            ) from exc

    def _deleted(self, response: HttpResponse) -> bool:
        """A 404 means the reply went between the lookup and the delete."""
        if response.status == 404:
            return False
        raise_for_write_failure(response, _API)
        return True

    def _parse(self, data: Any, review_id: str) -> ReviewReply:
        attrs = data["attributes"]
        state = _STATES.get(attrs["state"])
        if state is None:
            raise ValueError(f"unknown reply state {attrs['state']!r}")
        text = attrs["responseBody"]
        if not isinstance(text, str):
            raise TypeError(f"responseBody is {type(text).__name__}, expected a string")
        modified = attrs.get("lastModifiedDate")
        return ReviewReply(
            review_id=review_id,
            reply_id=str(data["id"]),
            text=text,
            state=state,
            updated_at=datetime.fromisoformat(modified) if modified else None,
        )
