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

import asyncio
import json
import threading
from typing import Any
from urllib.parse import quote

from app_reviews.core.classify import raise_for_http_failure, raise_for_write_failure
from app_reviews.core.client import PooledClient
from app_reviews.core.http import HttpClient, HttpResponse
from app_reviews.core.ratelimit import RequestLimiter
from app_reviews.errors import ParseError, ReplyRejectedError
from app_reviews.googleplay.auth import GoogleAuth
from app_reviews.googleplay.developer_api import protobuf_timestamp
from app_reviews.models.config import GooglePlayAuth, RetryConfig
from app_reviews.models.reply import ReviewReply

_API = "the Google Play Developer API"

MAX_REPLY_CHARS = 350
"""Google documents that replies over about 350 characters are rejected."""

_JSON_HEADERS = {"Content-Type": "application/json"}


class GooglePlayReplies(PooledClient):
    """Reads and writes the developer reply to a Google Play review.

    ``review_id`` is the Developer API ``reviewId``, which is ``Review.id`` on
    reviews fetched with ``GooglePlayReviews(auth=...)``. Play keys reviews by
    app, so every call takes the ``package_name`` too.

    ``reply`` is sent exactly once, whatever ``retry`` says: a reply is public,
    and a retried write whose first attempt did land cannot be recalled. When
    its outcome is unknown it raises ``ReplyOutcomeUnknownError``; read the state
    with ``get_reply()`` before trying again. Reads follow the normal retry
    policy. The OAuth token exchange before a write is not the write, so it
    retries as usual.

    Every method raises ``AuthError`` for a key that is unusable or lacks the
    permission, and ``RateLimitError`` (with ``retry_after``) for a 429.
    """

    URL_TEMPLATE = (
        "https://androidpublisher.googleapis.com"
        "/androidpublisher/v3/applications/{package}/reviews/{review}"
    )

    def __init__(
        self,
        auth: GooglePlayAuth,
        *,
        http: HttpClient | None = None,
        proxy: str | None = None,
        retry: RetryConfig | None = None,
        rate_limiter: RequestLimiter | None = None,
    ) -> None:
        super().__init__(proxy=proxy, retry=retry, http=http, rate_limiter=rate_limiter)
        self._auth = auth
        self._token: GoogleAuth | None = None
        self._token_lock = threading.Lock()

    def reply(self, review_id: str, text: str, *, package_name: str) -> ReviewReply:
        """Publish ``text`` as the reply to ``review_id``, replacing any existing one.

        Raises ``ReplyRejectedError(reason="too_long")`` without sending anything
        when ``text`` is over 350 characters, ``ReplyRejectedError`` when Google
        refuses it, and ``ReplyOutcomeUnknownError`` when it may or may not have
        been applied.
        """
        self._check_length(text)
        response = self._http.post(
            self._url(package_name, review_id) + ":reply",
            body=json.dumps({"replyText": text}),
            headers=self._headers() | _JSON_HEADERS,
            follow_redirects=False,
            retryable=False,
        )
        return self._written(response, review_id)

    async def areply(
        self, review_id: str, text: str, *, package_name: str
    ) -> ReviewReply:
        """Async equivalent of ``reply``."""
        self._check_length(text)
        response = await self._http.apost(
            self._url(package_name, review_id) + ":reply",
            body=json.dumps({"replyText": text}),
            headers=await self._aheaders() | _JSON_HEADERS,
            follow_redirects=False,
            retryable=False,
        )
        return self._written(response, review_id)

    def get_reply(self, review_id: str, *, package_name: str) -> ReviewReply | None:
        """The reply to ``review_id``, or None when it has none.

        Raises ``NotFoundError`` when Play has no such review.
        """
        response = self._http.get(
            self._url(package_name, review_id), headers=self._headers()
        )
        return self._read(response, review_id)

    async def aget_reply(
        self, review_id: str, *, package_name: str
    ) -> ReviewReply | None:
        """Async equivalent of ``get_reply``."""
        response = await self._http.aget(
            self._url(package_name, review_id), headers=await self._aheaders()
        )
        return self._read(response, review_id)

    def close(self) -> None:
        """Discard the cached token and close only a pool created here."""
        if self._token is not None:
            self._token.close()
        super().close()

    async def aclose(self) -> None:
        """Async equivalent of :meth:`close`."""
        if self._token is not None:
            await self._token.aclose()
        await super().aclose()

    def _check_length(self, text: str) -> None:
        if len(text) > MAX_REPLY_CHARS:
            raise ReplyRejectedError(
                f"Google Play rejects replies over {MAX_REPLY_CHARS} characters; "
                f"this one has {len(text)}. Nothing was sent.",
                reason="too_long",
            )

    def _url(self, package_name: str, review_id: str) -> str:
        """Both escaped: they land in the path of a request carrying the token."""
        return self.URL_TEMPLATE.format(
            package=quote(package_name, safe=""), review=quote(review_id, safe="")
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._google_auth().authorization_header()}

    async def _aheaders(self) -> dict[str, str]:
        token = self._token
        if token is None:
            token = await asyncio.to_thread(self._google_auth)
        return {"Authorization": await token.aauthorization_header()}

    def _google_auth(self) -> GoogleAuth:
        """Built at first use on this client's pool, so a key file is read then.

        Sharing the pool keeps the token exchange on the same proxy, retry policy
        and limiter as the reply requests.
        """
        if self._token is None:
            with self._token_lock:
                if self._token is None:
                    self._token = GoogleAuth(
                        self._auth.service_account_path,
                        service_account_info=self._auth.service_account_info,
                        http=self._http,
                    )
        return self._token

    def _written(self, response: HttpResponse, review_id: str) -> ReviewReply:
        raise_for_write_failure(response, _API)
        try:
            result = response.json()["result"]
            return self._reply(review_id, result["replyText"], result.get("lastEdited"))
        except (KeyError, OverflowError, OSError, TypeError, ValueError) as exc:
            raise ParseError(
                f"{_API} accepted the reply but its answer could not be read: {exc}",
                status=response.status,
            ) from exc

    def _read(self, response: HttpResponse, review_id: str) -> ReviewReply | None:
        raise_for_http_failure(response, _API)
        try:
            comments = response.json().get("comments", [])
            for comment in comments:
                developer = comment.get("developerComment")
                if developer is not None:
                    return self._reply(
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

    def _reply(self, review_id: str, text: Any, edited: Any) -> ReviewReply:
        if not isinstance(text, str):
            raise TypeError(f"reply text is {type(text).__name__}, expected a string")
        return ReviewReply(
            review_id=review_id,
            reply_id=None,
            text=text,
            state="published",
            updated_at=None if edited is None else protobuf_timestamp(edited),
        )
