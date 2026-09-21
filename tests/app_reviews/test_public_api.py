"""The public API contract."""

import app_reviews

EXPECTED = {
    "AppMetadata",
    "AppReviewsError",
    "AppStoreAuth",
    "AppStoreReviews",
    "AppStoreSearch",
    "AuthError",
    "Country",
    "CountryOutcome",
    "ErrorKind",
    "FetchError",
    "FetchResult",
    "GooglePlayAuth",
    "GooglePlayReviews",
    "GooglePlaySearch",
    "HttpClient",
    "HttpError",
    "NotFoundError",
    "ParseError",
    "RateLimitError",
    "RequestError",
    "PageResult",
    "RetryConfig",
    "Review",
    "ServerError",
    "Sort",
    "Source",
    "StopReason",
    "Store",
    "TransportError",
    "__version__",
}


class TestPublicApi:
    def test_all_matches_expected(self):
        assert set(app_reviews.__all__) == EXPECTED

    def test_all_is_sorted(self):
        assert app_reviews.__all__ == sorted(app_reviews.__all__)

    def test_every_name_is_importable(self):
        for name in app_reviews.__all__:
            assert getattr(app_reviews, name) is not None, name

    def test_utils_is_gone(self):
        import pytest

        with pytest.raises(ImportError):
            import app_reviews.utils  # noqa: F401

    def test_removed_config_models_are_gone(self):
        for name in ("ReviewConfig", "ExportConfig", "ProxyConfig", "load_config"):
            assert not hasattr(app_reviews, name)
            assert not hasattr(app_reviews.models, name)


class TestOneImportPathPerName:
    """The root is the API. Nothing else re-exports it.

    ``app_reviews.models`` used to mirror most of the root, so ``Review`` had
    three import paths and the middle one advertised four names the root did not
    including two credential classes a caller never constructs. Nothing
    imported through it, so it was surface with no consumers.
    """

    def test_models_is_not_a_second_public_surface(self):
        assert not hasattr(app_reviews.models, "__all__")

    def test_models_does_not_re_export_the_root_names(self):
        """Submodules do not count: ``models.country`` is the module that
        defines ``Country``, not a re-export of it."""
        import types as pytypes

        leaked = [
            n
            for n in app_reviews.__all__
            if hasattr(app_reviews.models, n)
            and not isinstance(getattr(app_reviews.models, n), pytypes.ModuleType)
        ]

        assert leaked == []

    def test_the_annotation_vocabularies_are_at_the_root(self):
        """``Source`` and ``Store`` were reachable only via ``models``, despite
        being exactly the kind of name you need to annotate a signature."""
        assert app_reviews.Source is not None
        assert app_reviews.Store is not None


class TestStoreModulesExposeTheirProviders:
    """What ``appstore``/``googleplay`` are *for*.

    Both re-export their two clients for symmetry, but the root already has
    those. The providers are the reason to reach into a store module: to drive
    one directly, or to pin a source instead of letting credentials pick it.
    """

    def test_appstore_exposes_both_providers(self):
        from app_reviews import appstore

        assert appstore.AppStoreScraperProvider.source == "appstore_scraper"
        assert appstore.AppStoreOfficialProvider.source == "appstore_official"

    def test_googleplay_exposes_both_providers(self):
        from app_reviews import googleplay

        assert googleplay.GooglePlayScraperProvider.source == "googleplay_scraper"
        assert googleplay.GooglePlayOfficialProvider.source == "googleplay_official"

    def test_core_is_not_advertised(self):
        """``core`` is the engine, not API; it exports nothing by name."""
        from app_reviews import core

        assert not hasattr(core, "__all__")


class TestErrorsAreCatchable:
    """A caller must be able to catch what this package raises without
    importing a private path. ``HttpError`` and ``AuthError`` were reachable
    only via ``app_reviews.errors``, which no documentation mentioned.
    """

    def test_the_exception_hierarchy_is_exported(self):
        assert issubclass(app_reviews.HttpError, app_reviews.AppReviewsError)
        assert issubclass(app_reviews.AuthError, app_reviews.AppReviewsError)

    def test_every_request_failure_is_catchable_as_http_error(self):
        """One taxonomy, two deliveries: ``search``/``lookup`` raise a class,
        ``fetch`` returns the matching ``kind`` inside a ``FetchError``. The
        specific classes are covered in ``tests/app_reviews/test_errors.py``."""
        for name in (
            "RateLimitError",
            "NotFoundError",
            "ServerError",
            "TransportError",
            "ParseError",
        ):
            assert issubclass(getattr(app_reviews, name), app_reviews.HttpError), name

    def test_status_is_the_only_payload(self):
        assert app_reviews.RateLimitError("boom", status=429).status == 429
        assert app_reviews.RateLimitError("boom").status is None


class TestLadderIsPublic:
    def test_reviews_clients_expose_all_six_rungs(self):
        for client in (app_reviews.AppStoreReviews, app_reviews.GooglePlayReviews):
            for method in (
                "fetch",
                "afetch",
                "iter_pages",
                "aiter_pages",
                "fetch_page",
                "afetch_page",
            ):
                assert hasattr(client, method), f"{client.__name__}.{method}"

    def test_search_clients_expose_async_variants(self):
        for client in (app_reviews.AppStoreSearch, app_reviews.GooglePlaySearch):
            for method in ("search", "asearch", "lookup", "alookup"):
                assert hasattr(client, method), f"{client.__name__}.{method}"


class TestOfficialProvidersAreConstructible:
    """The store modules document their providers as the reason to reach in, and
    the official ones need a ``TokenSource``. If no implementation is exported,
    the documented path dead-ends.
    """

    def test_appstore_exports_a_token_source(self):
        """``ConnectAuth`` has to satisfy ``TokenSource`` structurally, which is
        the actual contract: ``issubclass(x, object)`` was true of everything."""
        import inspect

        from app_reviews.appstore import AppStoreOfficialProvider, ConnectAuth
        from app_reviews.core.auth import TokenSource
        from app_reviews.models.config import ConnectCredentials

        # TokenSource is a plain Protocol, so isinstance is not available; what
        # actually matters is that both halves of the contract are present and
        # that the async one really is awaitable.
        #
        # The members are read off the annotations rather than
        # ``__protocol_attrs__``, which is a CPython implementation detail added
        # in 3.12 and absent on the 3.11 this package supports.
        required = {
            name
            for name, value in vars(TokenSource).items()
            if not name.startswith("_") and callable(value)
        }
        assert required == {"authorization_header", "aauthorization_header"}
        assert required <= set(dir(ConnectAuth))
        assert inspect.iscoroutinefunction(ConnectAuth.aauthorization_header)
        assert not inspect.iscoroutinefunction(ConnectAuth.authorization_header)
        assert inspect.iscoroutinefunction(AppStoreOfficialProvider.afetch_page)
        assert "private_key" in ConnectCredentials.__dataclass_fields__

    def test_googleplay_exports_a_token_source(self):
        from app_reviews.googleplay import GoogleAuth, GooglePlayOfficialProvider

        assert GoogleAuth is not None
        assert GooglePlayOfficialProvider is not None

    def test_a_caller_can_actually_build_the_official_provider(self):
        """The whole journey, using only names the store module exports."""
        from app_reviews import AppStoreAuth
        from app_reviews.appstore import AppStoreOfficialProvider, ConnectAuth
        from app_reviews.models.config import ConnectCredentials
        from tests.app_reviews.appstore.test_auth import _TEST_PRIVATE_KEY

        assert AppStoreAuth is not None
        creds = ConnectCredentials(
            key_id="K", issuer_id="I", private_key=_TEST_PRIVATE_KEY
        )
        provider = AppStoreOfficialProvider(ConnectAuth(creds))

        assert provider.source == "appstore_official"

    def test_both_stores_stay_symmetric(self):
        from app_reviews import appstore, googleplay

        assert len(appstore.__all__) == len(googleplay.__all__) == 5
