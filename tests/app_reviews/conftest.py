"""Fixtures shared across the unit tests."""

from __future__ import annotations

import locale
from collections.abc import Iterator

import pytest


@pytest.fixture
def german_time_locale() -> Iterator[None]:
    """Switch ``LC_TIME`` to German for one test, then restore it.

    An embedding application may run under any locale, and ``strptime``'s ``%a``
    and ``%b`` follow it. Skips where the machine has no German locale.
    """
    previous = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
    except locale.Error:
        pytest.skip("the de_DE.UTF-8 locale is not installed")
    yield
    locale.setlocale(locale.LC_TIME, previous)
