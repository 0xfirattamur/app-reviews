"""GooglePlayReplies against a mocked Developer API transport."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app_reviews import (
    AuthError,
    GooglePlayAuth,
    GooglePlayReplies,
    HttpClient,
    NotFoundError,
    ParseError,
    RateLimitError,
    ReplyOutcomeUnknownError,
    ReplyRejectedError,
    RetryConfig,
    ReviewReply,
)
from tests.app_reviews.googleplay.test_auth import _SERVICE_ACCOUNT_JSON

_REVIEWS = (
    "https://androidpublisher.googleapis.com/androidpublisher/v3"
    "/applications/com.example.app/reviews"
)


class _Api:
    """Answers the token exchange itself; queues answers for everything else."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self.token_exchanges = 0
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_exchanges += 1
            return httpx.Response(200, json={"access_token": "tok"})
        self.requests.append(request)
        return (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )


def _replies(handler, *, retries: int = 3) -> GooglePlayReplies:
    return GooglePlayReplies(
        GooglePlayAuth(service_account_info=_SERVICE_ACCOUNT_JSON),
        http=HttpClient(
            transport=httpx.MockTransport(handler),
            retry=RetryConfig(max_retries=retries, backoff_factor=0),
        ),
    )


def _reply_result(text="Thanks!") -> dict:
    return {"result": {"replyText": text, "lastEdited": {"seconds": "1790000000"}}}


def _review(*comments: dict) -> dict:
    return {"reviewId": "gp-1", "authorName": "A", "comments": list(comments)}


_USER = {"userComment": {"text": "Great", "starRating": 5}}


class TestReply:
    def test_posts_reply_text_and_returns_a_published_reply(self):
        api = _Api(httpx.Response(200, json=_reply_result()))

        reply = _replies(api).reply("gp-1", "Thanks!", package_name="com.example.app")

        request = api.requests[0]
        assert request.method == "POST"
        assert str(request.url) == f"{_REVIEWS}/gp-1:reply"
        assert request.headers["authorization"] == "Bearer tok"
        assert json.loads(request.content) == {"replyText": "Thanks!"}
        assert reply == ReviewReply(
            review_id="gp-1",
            reply_id=None,
            text="Thanks!",
            state="published",
            updated_at=datetime.fromtimestamp(1790000000, tz=UTC),
        )

    def test_exactly_350_characters_is_sent(self):
        api = _Api(httpx.Response(200, json=_reply_result("x" * 350)))

        _replies(api).reply("gp-1", "x" * 350, package_name="com.example.app")

        assert len(api.requests) == 1

    def test_351_characters_is_rejected_before_any_request(self):
        api = _Api(httpx.Response(200, json=_reply_result()))

        with pytest.raises(ReplyRejectedError) as caught:
            _replies(api).reply("gp-1", "x" * 351, package_name="com.example.app")

        assert caught.value.reason == "too_long"
        assert api.requests == []
        assert api.token_exchanges == 0

    async def test_the_async_pre_check_sends_nothing_either(self):
        api = _Api(httpx.Response(200, json=_reply_result()))

        with pytest.raises(ReplyRejectedError, match="351"):
            await _replies(api).areply(
                "gp-1", "x" * 351, package_name="com.example.app"
            )

        assert api.requests == []

    @pytest.mark.parametrize("status", [500, 503])
    def test_a_5xx_is_sent_once_and_its_outcome_is_unknown(self, status):
        api = _Api(httpx.Response(status))

        with pytest.raises(ReplyOutcomeUnknownError):
            _replies(api).reply("gp-1", "Thanks!", package_name="com.example.app")

        assert len(api.requests) == 1

    def test_a_dropped_connection_is_sent_once_and_its_outcome_is_unknown(self):
        calls = []

        def handler(request):
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "tok"})
            calls.append(request)
            raise httpx.RemoteProtocolError("peer closed", request=request)

        with pytest.raises(ReplyOutcomeUnknownError):
            _replies(handler).reply("gp-1", "Thanks!", package_name="com.example.app")

        assert len(calls) == 1

    async def test_the_async_write_is_not_retried(self):
        api = _Api(httpx.Response(502))

        with pytest.raises(ReplyOutcomeUnknownError):
            await _replies(api).areply("gp-1", "Hi", package_name="com.example.app")

        assert len(api.requests) == 1

    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_a_redirected_write_is_not_followed(self, asynchronous):
        """Following a 307/308 would make httpx post the reply a second time."""
        api = _Api(
            httpx.Response(307, headers={"Location": f"{_REVIEWS}/elsewhere"}),
            httpx.Response(200, json=_reply_result()),
        )
        replies = _replies(api)

        with pytest.raises(ReplyOutcomeUnknownError):
            if asynchronous:
                await replies.areply("gp-1", "Hi", package_name="com.example.app")
            else:
                replies.reply("gp-1", "Hi", package_name="com.example.app")

        assert len(api.requests) == 1

    def test_a_429_is_rate_limited_with_the_asked_wait(self):
        api = _Api(httpx.Response(429, headers={"Retry-After": "30"}))

        with pytest.raises(RateLimitError) as caught:
            _replies(api).reply("gp-1", "Hi", package_name="com.example.app")

        assert caught.value.retry_after == 30.0
        assert len(api.requests) == 1

    def test_a_403_without_the_permission_is_an_auth_error(self):
        body = {"error": {"code": 403, "status": "PERMISSION_DENIED"}}

        with pytest.raises(AuthError):
            _replies(_Api(httpx.Response(403, json=body))).reply(
                "gp-1", "Hi", package_name="com.example.app"
            )

    def test_a_4xx_refusal_carries_googles_status(self):
        body = {
            "error": {
                "code": 400,
                "message": "Reply text is too long.",
                "status": "INVALID_ARGUMENT",
            }
        }

        with pytest.raises(ReplyRejectedError) as caught:
            _replies(_Api(httpx.Response(400, json=body))).reply(
                "gp-1", "Hi", package_name="com.example.app"
            )

        assert caught.value.reason == "INVALID_ARGUMENT"
        assert "Reply text is too long." in str(caught.value)

    def test_an_accepted_reply_with_an_unreadable_answer_is_a_parse_error(self):
        with pytest.raises(ParseError, match="accepted"):
            _replies(_Api(httpx.Response(200, json={}))).reply(
                "gp-1", "Hi", package_name="com.example.app"
            )

    def test_the_package_and_review_cannot_retarget_the_request(self):
        api = _Api(httpx.Response(200, json=_reply_result()))

        _replies(api).reply("../x", "Hi", package_name="../../other")

        assert api.requests[0].url.raw_path == (
            b"/androidpublisher/v3/applications/..%2F..%2Fother/reviews/..%2Fx:reply"
        )


class TestGetReply:
    def test_reads_the_developer_comment(self):
        developer = {
            "developerComment": {"text": "Thanks!", "lastModified": {"seconds": "5"}}
        }
        api = _Api(httpx.Response(200, json=_review(_USER, developer)))

        reply = _replies(api).get_reply("gp-1", package_name="com.example.app")

        assert str(api.requests[0].url) == f"{_REVIEWS}/gp-1"
        assert reply == ReviewReply(
            review_id="gp-1",
            reply_id=None,
            text="Thanks!",
            state="published",
            updated_at=datetime.fromtimestamp(5, tz=UTC),
        )

    def test_a_review_without_a_developer_comment_is_none(self):
        api = _Api(httpx.Response(200, json=_review(_USER)))

        assert _replies(api).get_reply("gp-1", package_name="com.example.app") is None

    def test_an_unknown_review_is_not_found(self):
        with pytest.raises(NotFoundError):
            _replies(_Api(httpx.Response(404))).get_reply(
                "gp-1", package_name="com.example.app"
            )

    def test_reads_are_retried_under_the_normal_policy(self):
        api = _Api(httpx.Response(503), httpx.Response(200, json=_review(_USER)))

        assert _replies(api).get_reply("gp-1", package_name="com.example.app") is None
        assert len(api.requests) == 2

    def test_a_malformed_review_is_a_parse_error(self):
        api = _Api(httpx.Response(200, json={"comments": [{"developerComment": {}}]}))

        with pytest.raises(ParseError):
            _replies(api).get_reply("gp-1", package_name="com.example.app")

    async def test_async_get_reply_matches(self):
        developer = {"developerComment": {"text": "Hi"}}
        api = _Api(httpx.Response(200, json=_review(_USER, developer)))

        reply = await _replies(api).aget_reply("gp-1", package_name="com.example.app")

        assert reply is not None
        assert (reply.text, reply.updated_at) == ("Hi", None)


class TestCredentials:
    def test_an_unusable_key_fails_before_anything_is_sent(self):
        calls = []
        replies = GooglePlayReplies(
            GooglePlayAuth(service_account_info={"client_email": ""}),
            http=HttpClient(transport=httpx.MockTransport(calls.append)),
        )

        with pytest.raises(AuthError):
            replies.get_reply("gp-1", package_name="com.example.app")

        assert calls == []

    def test_one_token_serves_several_calls(self):
        api = _Api(httpx.Response(200, json=_review(_USER)))
        replies = _replies(api)

        replies.get_reply("gp-1", package_name="com.example.app")
        replies.get_reply("gp-2", package_name="com.example.app")

        assert api.token_exchanges == 1
