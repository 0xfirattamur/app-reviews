"""Replying to Google Play reviews through the Developer API.

Endpoints (Google Play Developer API v3, ``reviews``):

- ``POST .../applications/{packageName}/reviews/{reviewId}:reply`` sets the
  reply, or replaces the one already there:
  https://developers.google.com/android-publisher/api-ref/rest/v3/reviews/reply
- ``GET .../applications/{packageName}/reviews/{reviewId}`` reads the review,
  whose ``developerComment`` is the reply:
  https://developers.google.com/android-publisher/api-ref/rest/v3/reviews/get

Play has no API to delete a reply. The service account needs the Play Console
"Reply to reviews" permission for the app.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from app_reviews.core.classify import raise_for_http_failure
from app_reviews.core.http import HttpResponse
from app_reviews.errors import ParseError, ReplyRejectedError
from app_reviews.googleplay.api import PlayAPIClient
from app_reviews.googleplay.developer_api import protobuf_timestamp
from app_reviews.models.reply import ReviewReply
from app_reviews.reply_failure import StoreError, json_object, raise_for_reply_failure

_API = "the Google Play Developer API"

MAX_REPLY_CHARS = 350
"""Google documents that replies over about 350 characters are rejected."""

_JSON_HEADERS = {"Content-Type": "application/json"}


class GooglePlayReplies(PlayAPIClient):
    """Reads and writes the developer reply to a Google Play review.

    ``review_id`` is ``Review.id`` from ``GooglePlayReviews(auth=...)``; Play
    keys reviews by app, so every call takes ``package_name`` too. ``reply`` is
    sent once (see ``HttpClient.send_once``); reads and the token exchange retry
    as configured. Raises ``AuthError`` for a key without the permission,
    ``RateLimitError`` for a 429.
    """

    def reply(self, review_id: str, text: str, *, package_name: str) -> ReviewReply:
        """Publish ``text`` as the reply to ``review_id``, replacing any existing one.

        Raises ``ReplyRejectedError(reason="too_long")`` without sending when
        ``text`` is over 350 characters, ``ReplyRejectedError`` when Google
        refuses it, ``ReplyOutcomeUnknownError`` when it may or may not have
        applied.
        """
        _check_length(text)
        response = self._http.send_once(
            "POST",
            self._review_url(package_name, review_id) + ":reply",
            body=json.dumps({"replyText": text}),
            headers=self._headers() | _JSON_HEADERS,
        )
        return self._written(response, review_id)

    async def areply(
        self, review_id: str, text: str, *, package_name: str
    ) -> ReviewReply:
        """Async equivalent of ``reply``."""
        _check_length(text)
        response = await self._http.asend_once(
            "POST",
            self._review_url(package_name, review_id) + ":reply",
            body=json.dumps({"replyText": text}),
            headers=await self._aheaders() | _JSON_HEADERS,
        )
        return self._written(response, review_id)

    def get_reply(self, review_id: str, *, package_name: str) -> ReviewReply | None:
        """The reply to ``review_id``, or None. ``NotFoundError`` for no such review."""
        response = self._http.get(
            self._review_url(package_name, review_id), headers=self._headers()
        )
        return self._read(response, review_id)

    async def aget_reply(
        self, review_id: str, *, package_name: str
    ) -> ReviewReply | None:
        """Async equivalent of ``get_reply``."""
        response = await self._http.aget(
            self._review_url(package_name, review_id), headers=await self._aheaders()
        )
        return self._read(response, review_id)

    def _review_url(self, package_name: str, review_id: str) -> str:
        package, review = quote(package_name, safe=""), quote(review_id, safe="")
        return f"{self.API_BASE}/applications/{package}/reviews/{review}"

    def _written(self, response: HttpResponse, review_id: str) -> ReviewReply:
        raise_for_reply_failure(response, _API, _store_error)
        try:
            result = response.json()["result"]
            return _reply(review_id, result["replyText"], result.get("lastEdited"))
        except (KeyError, OverflowError, OSError, TypeError, ValueError) as exc:
            raise ParseError(
                f"{_API} accepted the reply but its answer could not be read: {exc}",
                status=response.status,
            ) from exc

    def _read(self, response: HttpResponse, review_id: str) -> ReviewReply | None:
        """The review's ``developerComment``, if it has one."""
        raise_for_http_failure(response, _API)
        try:
            for comment in response.json().get("comments", []):
                developer = comment.get("developerComment")
                if developer is not None:
                    return _reply(
                        review_id, developer["text"], developer.get("lastModified")
                    )
        except (
            AttributeError,
            KeyError,
            OverflowError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            raise ParseError(
                f"Malformed review from {_API}: {exc}", status=response.status
            ) from exc
        return None


def _check_length(text: str) -> None:
    if len(text) > MAX_REPLY_CHARS:
        raise ReplyRejectedError(
            f"Google Play rejects replies over {MAX_REPLY_CHARS} characters; "
            f"this one has {len(text)}. Nothing was sent.",
            reason="too_long",
        )


def _reply(review_id: str, text: Any, edited: Any) -> ReviewReply:
    if not isinstance(text, str):
        raise TypeError(f"reply text is {type(text).__name__}, expected a string")
    return ReviewReply(
        review_id=review_id,
        reply_id=None,
        text=text,
        state="published",
        updated_at=None if edited is None else protobuf_timestamp(edited),
    )


def _store_error(body: str) -> StoreError:
    """Google API errors: ``{"error": {"status": ..., "message": ...}}``."""
    entry = json_object(body).get("error")
    if not isinstance(entry, dict):
        return StoreError()
    return StoreError.of(entry.get("status"), entry.get("message"))
