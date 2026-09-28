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
from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.http import HttpResponse
from app_reviews.errors import ParseError
from app_reviews.models.reply import ReviewReply
from app_reviews.models.types import ReplyState
from app_reviews.reply_failure import StoreError, json_object, raise_for_reply_failure

_API = "the App Store Connect API"

_STATES: dict[str, ReplyState] = {
    "PUBLISHED": "published",
    "PENDING_PUBLISH": "pending",
}
"""Apple's documented ``CustomerReviewResponseV1.state`` values."""

_JSON_HEADERS = {"Content-Type": "application/json"}


class AppStoreReplies(ConnectAPIClient):
    """Reads, writes and deletes the developer reply to an App Store review.

    ``review_id`` is ``Review.id`` from ``AppStoreReviews(auth=...)``. Writes are
    sent once (see ``HttpClient.send_once``); reads retry as configured.
    Raises ``AuthError`` for a key without the role, ``RateLimitError`` for a 429.
    """

    def reply(self, review_id: str, text: str) -> ReviewReply:
        """Publish ``text`` as the reply to ``review_id``, replacing any existing one.

        Apple usually answers ``state="pending"``: it can take 24 hours to show.
        Raises ``ReplyRejectedError`` when refused, ``ReplyOutcomeUnknownError``
        when it may or may not have applied.
        """
        response = self._http.send_once(
            "POST",
            self._replies_url(),
            body=_reply_body(review_id, text),
            headers=self._headers() | _JSON_HEADERS,
        )
        return self._written(response, review_id)

    async def areply(self, review_id: str, text: str) -> ReviewReply:
        """Async equivalent of ``reply``."""
        response = await self._http.asend_once(
            "POST",
            self._replies_url(),
            body=_reply_body(review_id, text),
            headers=await self._aheaders() | _JSON_HEADERS,
        )
        return self._written(response, review_id)

    def get_reply(self, review_id: str) -> ReviewReply | None:
        """The reply to ``review_id``, or None. ``NotFoundError`` for no such review."""
        response = self._http.get(
            self._review_reply_url(review_id), headers=self._headers()
        )
        return self._read(response, review_id)

    async def aget_reply(self, review_id: str) -> ReviewReply | None:
        """Async equivalent of ``get_reply``."""
        response = await self._http.aget(
            self._review_reply_url(review_id), headers=await self._aheaders()
        )
        return self._read(response, review_id)

    def delete_reply(self, review_id: str) -> bool:
        """Remove the reply to ``review_id``. False when there was none.

        Apple deletes by the reply's own id, so the reply is looked up first.
        """
        existing = self.get_reply(review_id)
        if existing is None or existing.reply_id is None:
            return False
        response = self._http.send_once(
            "DELETE", self._replies_url(existing.reply_id), headers=self._headers()
        )
        return self._deleted(response)

    async def adelete_reply(self, review_id: str) -> bool:
        """Async equivalent of ``delete_reply``."""
        existing = await self.aget_reply(review_id)
        if existing is None or existing.reply_id is None:
            return False
        response = await self._http.asend_once(
            "DELETE",
            self._replies_url(existing.reply_id),
            headers=await self._aheaders(),
        )
        return self._deleted(response)

    def _replies_url(self, reply_id: str | None = None) -> str:
        url = f"{self.API_BASE}/v1/customerReviewResponses"
        return url if reply_id is None else f"{url}/{quote(reply_id, safe='')}"

    def _review_reply_url(self, review_id: str) -> str:
        return (
            f"{self.API_BASE}/v1/customerReviews/{quote(review_id, safe='')}/response"
        )

    def _written(self, response: HttpResponse, review_id: str) -> ReviewReply:
        raise_for_reply_failure(response, _API, _store_error)
        try:
            return _reply(response.json()["data"], review_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(
                f"{_API} accepted the reply but its answer could not be read: {exc}",
                status=response.status,
            ) from exc

    def _read(self, response: HttpResponse, review_id: str) -> ReviewReply | None:
        """``data: null`` is a review without a reply."""
        raise_for_http_failure(response, _API)
        try:
            data = response.json()["data"]
            return None if data is None else _reply(data, review_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise ParseError(
                f"Malformed reply from {_API}: {exc}", status=response.status
            ) from exc

    def _deleted(self, response: HttpResponse) -> bool:
        """A 404 means the reply went between the lookup and the delete."""
        if response.status == 404:
            return False
        raise_for_reply_failure(response, _API, _store_error)
        return True


def _reply_body(review_id: str, text: str) -> str:
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


def _reply(data: Any, review_id: str) -> ReviewReply:
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


def _store_error(body: str) -> StoreError:
    """App Store Connect errors: ``{"errors": [{"code": ..., "detail": ...}]}``."""
    errors = json_object(body).get("errors")
    entry = errors[0] if isinstance(errors, list) and errors else None
    if not isinstance(entry, dict):
        return StoreError()
    return StoreError.of(entry.get("code"), entry.get("detail"))
