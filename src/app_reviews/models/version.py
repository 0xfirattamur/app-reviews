"""One App Store version as App Store Connect records it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AppStoreVersion:
    """One ``appStoreVersions`` resource, with its "What's New" text per locale.

    The official API has no release date: App Store Connect does not record when
    a version went live, so there is no release date here. The two dates it does
    have mean something else:

    - ``created_at`` (``createdDate``) is when the version was created in App
      Store Connect, usually days or weeks before it shipped.
    - ``earliest_release_date`` (``earliestReleaseDate``) is the earliest moment
      a ``SCHEDULED`` release may go out, as the developer set it; None for the
      other release types.

    For the dates versions actually reached the store, use
    ``AppStoreSearch.version_history()``, which reads the public product page.

    ``state`` is ``appVersionState``, which tells whether a version is live:
    ``"READY_FOR_DISTRIBUTION"`` once it is on the store, otherwise a stage such
    as ``"PREPARE_FOR_SUBMISSION"`` or ``"WAITING_FOR_REVIEW"``.
    ``release_notes`` maps each locale (``"en-US"``) to its ``whatsNew`` text;
    locales without one are left out.
    """

    version_id: str
    version: str
    platform: str
    state: str | None
    release_type: str | None
    created_at: datetime | None
    earliest_release_date: datetime | None
    release_notes: dict[str, str] = field(default_factory=dict)
