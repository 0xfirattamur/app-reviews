"""AppStoreReplies against a mocked App Store Connect transport."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest

from app_reviews import (
    AppStoreAuth,
    AppStoreReplies,
    AuthError,
    HttpClient,
    NotFoundError,
    ParseError,
    RateLimitError,
    ReplyOutcomeUnknownError,
    ReplyRejectedError,
    ReviewReply,
    ServerError,
)
from tests.app_reviews.factories import TEST_EC_PRIVATE_KEY, connect_credentials
from tests.app_reviews.transport import Recorder, client_for

_BASE = "https://api.appstoreconnect.apple.com/v1"


def _response_doc(
    *, reply_id="resp-1", body="Thanks!", state="PENDING_PUBLISH"
) -> dict:
    return {
        "data": {
            "type": "customerReviewResponses",
            "id": reply_id,
            "attributes": {
                "responseBody": body,
                "lastModifiedDate": "2026-09-28T10:00:00-07:00",
                "state": state,
            },
        }
    }


def _replies(handler, *, retries: int = 3) -> AppStoreReplies:
    auth = AppStoreAuth(key_id="K", issuer_id="I", private_key=TEST_EC_PRIVATE_KEY)
    return client_for(AppStoreReplies, handler, retries=retries, auth=auth)


class TestReply:
    def test_posts_the_documented_body_and_returns_a_pending_reply(self):
        record = Recorder(httpx.Response(201, json=_response_doc()))

        reply = _replies(record).reply("review-9", "Thanks!")

        request = record.requests[0]
        assert request.method == "POST"
        assert str(request.url) == f"{_BASE}/customerReviewResponses"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "data": {
                "type": "customerReviewResponses",
                "attributes": {"responseBody": "Thanks!"},
                "relationships": {
                    "review": {"data": {"type": "customerReviews", "id": "review-9"}}
                },
            }
        }
        assert reply == ReviewReply(
            review_id="review-9",
            reply_id="resp-1",
            text="Thanks!",
            state="pending",
            updated_at=datetime(2026, 9, 28, 17, 0, tzinfo=UTC),
        )

    async def test_async_reply_matches(self):
        record = Recorder(httpx.Response(201, json=_response_doc(state="PUBLISHED")))

        reply = await _replies(record).areply("review-9", "Thanks!")

        assert reply.state == "published"
        assert record.requests[0].method == "POST"

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_a_5xx_is_sent_once_and_its_outcome_is_unknown(self, status):
        record = Recorder(httpx.Response(status))

        with pytest.raises(ReplyOutcomeUnknownError) as caught:
            _replies(record).reply("review-9", "Thanks!")

        assert len(record.requests) == 1
        assert caught.value.status == status

    def test_a_timeout_is_sent_once_and_its_outcome_is_unknown(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("timed out", request=request)

        with pytest.raises(ReplyOutcomeUnknownError, match="get_reply"):
            _replies(handler).reply("review-9", "Thanks!")

        assert len(calls) == 1

    async def test_the_async_write_is_not_retried_either(self):
        record = Recorder(httpx.Response(503))

        with pytest.raises(ReplyOutcomeUnknownError):
            await _replies(record).areply("review-9", "Thanks!")

        assert len(record.requests) == 1

    @pytest.mark.parametrize("status", [301, 302, 307, 308])
    def test_a_redirected_write_is_not_followed_and_its_outcome_is_unknown(
        self, status
    ):
        """Following a 307/308 would make httpx send the reply a second time."""
        record = Recorder(
            httpx.Response(status, headers={"Location": f"{_BASE}/elsewhere"}),
            httpx.Response(201, json=_response_doc()),
        )

        with pytest.raises(ReplyOutcomeUnknownError) as caught:
            _replies(record).reply("review-9", "Thanks!")

        assert len(record.requests) == 1
        assert caught.value.status == status

    async def test_the_async_write_does_not_follow_a_redirect_either(self):
        record = Recorder(
            httpx.Response(307, headers={"Location": f"{_BASE}/elsewhere"}),
            httpx.Response(201, json=_response_doc()),
        )

        with pytest.raises(ReplyOutcomeUnknownError):
            await _replies(record).areply("review-9", "Thanks!")

        assert len(record.requests) == 1

    def test_a_429_is_rate_limited_with_the_asked_wait_and_not_retried(self):
        record = Recorder(httpx.Response(429, headers={"Retry-After": "12"}))

        with pytest.raises(RateLimitError) as caught:
            _replies(record).reply("review-9", "Thanks!")

        assert caught.value.retry_after == 12.0
        assert len(record.requests) == 1

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_credential_is_an_auth_error(self, status):
        with pytest.raises(AuthError):
            _replies(Recorder(httpx.Response(status))).reply("review-9", "Thanks!")

    def test_a_4xx_refusal_carries_apples_error_code(self):
        body = {
            "errors": [
                {
                    "status": "409",
                    "code": "ENTITY_ERROR.ATTRIBUTE.INVALID",
                    "detail": "responseBody is too long",
                }
            ]
        }

        with pytest.raises(ReplyRejectedError) as caught:
            _replies(Recorder(httpx.Response(409, json=body))).reply("r", "x")

        assert caught.value.reason == "ENTITY_ERROR.ATTRIBUTE.INVALID"
        assert caught.value.status == 409
        assert "responseBody is too long" in str(caught.value)

    def test_a_4xx_without_a_json_body_is_rejected_by_status(self):
        with pytest.raises(ReplyRejectedError) as caught:
            _replies(Recorder(httpx.Response(422, text="<html>"))).reply("r", "x")

        assert caught.value.reason == "http_422"

    def test_an_accepted_reply_with_an_unreadable_answer_is_a_parse_error(self):
        with pytest.raises(ParseError, match="accepted"):
            _replies(Recorder(httpx.Response(201, text="{}"))).reply("r", "x")

    def test_an_unusable_key_fails_before_anything_is_sent(self):
        calls = []
        replies = AppStoreReplies(
            AppStoreAuth(key_id="K", issuer_id="I", private_key="not a key"),
            http=HttpClient(transport=httpx.MockTransport(calls.append)),
        )

        with pytest.raises(AuthError):
            replies.reply("r", "x")

        assert calls == []


class TestGetReply:
    def test_reads_the_reviews_response(self):
        record = Recorder(httpx.Response(200, json=_response_doc(state="PUBLISHED")))

        reply = _replies(record).get_reply("review-9")

        assert (
            str(record.requests[0].url) == f"{_BASE}/customerReviews/review-9/response"
        )
        assert reply is not None
        assert (reply.reply_id, reply.text, reply.state) == (
            "resp-1",
            "Thanks!",
            "published",
        )

    def test_a_review_without_a_reply_is_none(self):
        """What Connect answers for a real review nobody has replied to."""
        response = httpx.Response(200, json={"data": None, "links": {}})

        assert _replies(Recorder(response)).get_reply("review-9") is None

    def test_an_unknown_review_is_not_found(self):
        body = {"errors": [{"status": "404", "code": "NOT_FOUND"}]}

        with pytest.raises(NotFoundError):
            _replies(Recorder(httpx.Response(404, json=body))).get_reply("nope")

    def test_the_review_id_cannot_retarget_the_request(self):
        record = Recorder(httpx.Response(200, json={"data": None}))

        _replies(record).get_reply("../apps/1")

        assert record.requests[0].url.raw_path == (
            b"/v1/customerReviews/..%2Fapps%2F1/response"
        )

    def test_reads_are_retried_under_the_normal_policy(self):
        record = Recorder(
            httpx.Response(503),
            httpx.Response(200, json=_response_doc(state="PUBLISHED")),
        )

        reply = _replies(record).get_reply("review-9")

        assert reply is not None
        assert len(record.requests) == 2

    def test_a_read_that_keeps_failing_is_a_server_error(self):
        with pytest.raises(ServerError):
            _replies(Recorder(httpx.Response(503)), retries=1).get_reply("r")

    def test_a_429_read_reports_retry_after(self):
        record = Recorder(httpx.Response(429, headers={"Retry-After": "7"}))

        with pytest.raises(RateLimitError) as caught:
            _replies(record, retries=0).get_reply("r")

        assert caught.value.retry_after == 7.0

    def test_an_unknown_state_is_a_parse_error(self):
        record = Recorder(httpx.Response(200, json=_response_doc(state="HIDDEN")))

        with pytest.raises(ParseError, match="HIDDEN"):
            _replies(record).get_reply("r")

    async def test_async_get_reply_matches(self):
        record = Recorder(httpx.Response(200, json=_response_doc()))

        reply = await _replies(record).aget_reply("review-9")

        assert reply is not None and reply.state == "pending"


class TestDeleteReply:
    def test_deletes_by_the_replys_own_id(self):
        record = Recorder(
            httpx.Response(200, json=_response_doc(reply_id="resp-7")),
            httpx.Response(204),
        )

        assert _replies(record).delete_reply("review-9") is True

        get, delete = record.requests
        assert get.method == "GET"
        assert delete.method == "DELETE"
        assert str(delete.url) == f"{_BASE}/customerReviewResponses/resp-7"

    def test_nothing_to_delete_sends_no_delete(self):
        record = Recorder(httpx.Response(200, json={"data": None}))

        assert _replies(record).delete_reply("review-9") is False
        assert [r.method for r in record.requests] == ["GET"]

    def test_a_reply_gone_before_the_delete_is_false(self):
        record = Recorder(
            httpx.Response(200, json=_response_doc()), httpx.Response(404)
        )

        assert _replies(record).delete_reply("review-9") is False

    def test_a_5xx_delete_is_sent_once_and_its_outcome_is_unknown(self):
        record = Recorder(
            httpx.Response(200, json=_response_doc()), httpx.Response(503)
        )

        with pytest.raises(ReplyOutcomeUnknownError):
            _replies(record).delete_reply("review-9")

        assert [r.method for r in record.requests] == ["GET", "DELETE"]

    @pytest.mark.parametrize("asynchronous", [False, True])
    async def test_a_redirected_delete_is_not_followed(self, asynchronous):
        record = Recorder(
            httpx.Response(200, json=_response_doc()),
            httpx.Response(307, headers={"Location": f"{_BASE}/elsewhere"}),
            httpx.Response(204),
        )
        replies = _replies(record)

        with pytest.raises(ReplyOutcomeUnknownError):
            if asynchronous:
                await replies.adelete_reply("review-9")
            else:
                replies.delete_reply("review-9")

        assert [r.method for r in record.requests] == ["GET", "DELETE"]

    async def test_async_delete_matches(self):
        record = Recorder(
            httpx.Response(200, json=_response_doc()), httpx.Response(204)
        )

        assert await _replies(record).adelete_reply("review-9") is True
        assert record.requests[-1].method == "DELETE"


class TestTheSigningKeyLifecycle:
    """``AppStoreReplies`` loads its key on the first request, keeps one bearer
    token across calls, and drops that token when the client is closed."""

    async def test_one_token_serves_several_calls(self, twin, fake_clock):
        record = Recorder(httpx.Response(200, json={"data": None}))
        replies = _replies(record)

        await twin.call(replies, "get_reply", "review-1")
        fake_clock.advance(1)
        await twin.call(replies, "get_reply", "review-2")

        first, second = (r.headers["authorization"] for r in record.requests)
        assert first == second

    async def test_closing_drops_the_token(self, twin, fake_clock):
        record = Recorder(httpx.Response(200, json={"data": None}))
        replies = _replies(record)
        await twin.call(replies, "get_reply", "review-1")

        await twin.call(replies, "close")
        fake_clock.advance(1)
        await twin.call(replies, "get_reply", "review-2")

        first, second = (r.headers["authorization"] for r in record.requests)
        assert first != second

    async def test_closing_before_any_request_is_safe(self, twin):
        record = Recorder(httpx.Response(200, json={"data": None}))
        replies = _replies(record)

        await twin.call(replies, "close")

        assert record.requests == []


class TestTheKeyIsLoadedOnceUnderConcurrentFirstRequests:
    def test_two_threads_asking_at_once_share_one_load(self, monkeypatch):
        """The key file is read once, however many threads make the first call."""
        loads: list[object] = []
        started = threading.Event()
        release = threading.Event()

        def slow_load(auth):
            loads.append(auth)
            started.set()
            assert release.wait(timeout=2)
            return connect_credentials()

        monkeypatch.setattr(
            "app_reviews.appstore.api.load_connect_credentials", slow_load
        )
        replies = _replies(Recorder(httpx.Response(200, json={"data": None})))

        with ThreadPoolExecutor(max_workers=2) as threads:
            first = threads.submit(replies.get_reply, "review-1")
            assert started.wait(timeout=2)
            second = threads.submit(replies.get_reply, "review-2")
            time.sleep(0.05)  # let the second thread reach the loader's lock
            release.set()
            first.result(timeout=2)
            second.result(timeout=2)

        assert len(loads) == 1
