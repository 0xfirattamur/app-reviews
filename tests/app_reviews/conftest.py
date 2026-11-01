"""Fixtures shared across the unit tests."""

from __future__ import annotations

import itertools
import locale
from collections.abc import Callable, Iterator
from pathlib import Path

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


@pytest.fixture
def write_file(tmp_path: Path) -> Callable[[str], str]:
    """Write text to a new file under ``tmp_path`` and return its path.

    For tests that hand a path to code reading a key file. Each call makes a
    distinct file; pytest removes the directory with the test's other temp data.
    """
    names = (f"key-{number}" for number in itertools.count())

    def write(contents: str) -> str:
        path = tmp_path / next(names)
        path.write_text(contents)
        return str(path)

    return write
