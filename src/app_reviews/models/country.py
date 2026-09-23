"""Country StrEnum with region groups and display utilities."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import ClassVar

_LOG = logging.getLogger(__name__)


class Country(StrEnum):
    """iTunes-supported countries as a StrEnum.

    Values are lowercase ISO 3166-1 alpha-2 codes.
    """

    AE = "ae"
    AG = "ag"
    AI = "ai"
    AL = "al"
    AM = "am"
    AO = "ao"
    AR = "ar"
    AT = "at"
    AU = "au"
    AZ = "az"
    BB = "bb"
    BE = "be"
    BF = "bf"
    BG = "bg"
    BH = "bh"
    BJ = "bj"
    BM = "bm"
    BN = "bn"
    BO = "bo"
    BR = "br"
    BS = "bs"
    BT = "bt"
    BW = "bw"
    BY = "by"
    BZ = "bz"
    CA = "ca"
    CG = "cg"
    CH = "ch"
    CL = "cl"
    CN = "cn"
    CO = "co"
    CR = "cr"
    CV = "cv"
    CY = "cy"
    CZ = "cz"
    DE = "de"
    DK = "dk"
    DM = "dm"
    DO = "do"
    DZ = "dz"
    EC = "ec"
    EE = "ee"
    EG = "eg"
    ES = "es"
    FI = "fi"
    FJ = "fj"
    FM = "fm"
    FR = "fr"
    GB = "gb"
    GD = "gd"
    GH = "gh"
    GM = "gm"
    GR = "gr"
    GT = "gt"
    GW = "gw"
    GY = "gy"
    HK = "hk"
    HN = "hn"
    HR = "hr"
    HU = "hu"
    ID = "id"
    IE = "ie"
    IL = "il"
    IN = "in"
    IS = "is"
    IT = "it"
    JM = "jm"
    JO = "jo"
    JP = "jp"
    KE = "ke"
    KG = "kg"
    KH = "kh"
    KN = "kn"
    KR = "kr"
    KW = "kw"
    KY = "ky"
    KZ = "kz"
    LA = "la"
    LB = "lb"
    LC = "lc"
    LK = "lk"
    LR = "lr"
    LT = "lt"
    LU = "lu"
    LV = "lv"
    MD = "md"
    MG = "mg"
    MK = "mk"
    ML = "ml"
    MN = "mn"
    MO = "mo"
    MR = "mr"
    MS = "ms"
    MT = "mt"
    MU = "mu"
    MW = "mw"
    MX = "mx"
    MY = "my"
    MZ = "mz"
    NA = "na"
    NE = "ne"
    NG = "ng"
    NI = "ni"
    NL = "nl"
    NP = "np"
    NO = "no"
    NZ = "nz"
    OM = "om"
    PA = "pa"
    PE = "pe"
    PG = "pg"
    PH = "ph"
    PK = "pk"
    PL = "pl"
    PT = "pt"
    PW = "pw"
    PY = "py"
    QA = "qa"
    RO = "ro"
    RU = "ru"
    SA = "sa"
    SB = "sb"
    SC = "sc"
    SE = "se"
    SG = "sg"
    SI = "si"
    SK = "sk"
    SL = "sl"
    SN = "sn"
    SR = "sr"
    ST = "st"
    SV = "sv"
    SZ = "sz"
    TC = "tc"
    TD = "td"
    TH = "th"
    TJ = "tj"
    TM = "tm"
    TN = "tn"
    TR = "tr"
    TT = "tt"
    TW = "tw"
    TZ = "tz"
    UA = "ua"
    UG = "ug"
    US = "us"
    UY = "uy"
    UZ = "uz"
    VC = "vc"
    VE = "ve"
    VG = "vg"
    VN = "vn"
    YE = "ye"
    ZA = "za"
    ZW = "zw"

    # Region group declarations (populated after class body)
    ALL: ClassVar[frozenset[Country]]
    EUROPE: ClassVar[frozenset[Country]]
    AMERICAS: ClassVar[frozenset[Country]]
    ASIA_PACIFIC: ClassVar[frozenset[Country]]
    MIDDLE_EAST: ClassVar[frozenset[Country]]
    ENGLISH_SPEAKING: ClassVar[frozenset[Country]]


# Region groups, assigned after class creation because enum members
# must be defined before they can be referenced in frozenset literals.

Country.ALL = frozenset(Country)

Country.EUROPE = frozenset(
    {
        Country.AL,
        Country.AT,
        Country.BE,
        Country.BG,
        Country.BY,
        Country.CH,
        Country.CY,
        Country.CZ,
        Country.DE,
        Country.DK,
        Country.EE,
        Country.ES,
        Country.FI,
        Country.FR,
        Country.GB,
        Country.GR,
        Country.HR,
        Country.HU,
        Country.IE,
        Country.IS,
        Country.IT,
        Country.LT,
        Country.LU,
        Country.LV,
        Country.MD,
        Country.MK,
        Country.MT,
        Country.NL,
        Country.NO,
        Country.PL,
        Country.PT,
        Country.RO,
        Country.RU,
        Country.SE,
        Country.SI,
        Country.SK,
        Country.TR,
        Country.UA,
    }
)

Country.AMERICAS = frozenset(
    {
        Country.AG,
        Country.AI,
        Country.AR,
        Country.BB,
        Country.BM,
        Country.BO,
        Country.BR,
        Country.BS,
        Country.BZ,
        Country.CA,
        Country.CL,
        Country.CO,
        Country.CR,
        Country.DM,
        Country.DO,
        Country.EC,
        Country.GD,
        Country.GT,
        Country.GY,
        Country.HN,
        Country.JM,
        Country.KN,
        Country.KY,
        Country.LC,
        Country.MX,
        Country.NI,
        Country.PA,
        Country.PE,
        Country.PY,
        Country.SR,
        Country.SV,
        Country.TC,
        Country.TT,
        Country.US,
        Country.UY,
        Country.VC,
        Country.VE,
        Country.VG,
    }
)

Country.ASIA_PACIFIC = frozenset(
    {
        Country.AU,
        Country.BN,
        Country.BT,
        Country.CN,
        Country.FJ,
        Country.FM,
        Country.HK,
        Country.ID,
        Country.IN,
        Country.JP,
        Country.KH,
        Country.KR,
        Country.KZ,
        Country.KG,
        Country.LA,
        Country.LK,
        Country.MN,
        Country.MO,
        Country.MY,
        Country.NP,
        Country.NZ,
        Country.PG,
        Country.PH,
        Country.PK,
        Country.PW,
        Country.SB,
        Country.SG,
        Country.TH,
        Country.TJ,
        Country.TM,
        Country.TW,
        Country.UZ,
        Country.VN,
    }
)

Country.MIDDLE_EAST = frozenset(
    {
        Country.AE,
        Country.BH,
        Country.DZ,
        Country.EG,
        Country.IL,
        Country.JO,
        Country.KW,
        Country.LB,
        Country.OM,
        Country.QA,
        Country.SA,
        Country.TN,
        Country.YE,
    }
)

Country.ENGLISH_SPEAKING = frozenset(
    {
        Country.AU,
        Country.CA,
        Country.GB,
        Country.IE,
        Country.NZ,
        Country.US,
        Country.ZA,
        Country.JM,
        Country.TT,
        Country.BB,
        Country.BZ,
        Country.GY,
        Country.SG,
        Country.PH,
        Country.IN,
        Country.KE,
        Country.NG,
        Country.GH,
    }
)


_ALPHA3_TO_ALPHA2: dict[str, str] = {
    "ARE": "ae",
    "ATG": "ag",
    "AIA": "ai",
    "ALB": "al",
    "ARM": "am",
    "AGO": "ao",
    "ARG": "ar",
    "AUT": "at",
    "AUS": "au",
    "AZE": "az",
    "BRB": "bb",
    "BEL": "be",
    "BFA": "bf",
    "BGR": "bg",
    "BHR": "bh",
    "BEN": "bj",
    "BMU": "bm",
    "BRN": "bn",
    "BOL": "bo",
    "BRA": "br",
    "BHS": "bs",
    "BTN": "bt",
    "BWA": "bw",
    "BLR": "by",
    "BLZ": "bz",
    "CAN": "ca",
    "COG": "cg",
    "CHE": "ch",
    "CHL": "cl",
    "CHN": "cn",
    "COL": "co",
    "CRI": "cr",
    "CPV": "cv",
    "CYP": "cy",
    "CZE": "cz",
    "DEU": "de",
    "DNK": "dk",
    "DMA": "dm",
    "DOM": "do",
    "DZA": "dz",
    "ECU": "ec",
    "EST": "ee",
    "EGY": "eg",
    "ESP": "es",
    "FIN": "fi",
    "FJI": "fj",
    "FSM": "fm",
    "FRA": "fr",
    "GBR": "gb",
    "GRD": "gd",
    "GHA": "gh",
    "GMB": "gm",
    "GRC": "gr",
    "GTM": "gt",
    "GNB": "gw",
    "GUY": "gy",
    "HKG": "hk",
    "HND": "hn",
    "HRV": "hr",
    "HUN": "hu",
    "IDN": "id",
    "IRL": "ie",
    "ISR": "il",
    "IND": "in",
    "ISL": "is",
    "ITA": "it",
    "JAM": "jm",
    "JOR": "jo",
    "JPN": "jp",
    "KEN": "ke",
    "KGZ": "kg",
    "KHM": "kh",
    "KNA": "kn",
    "KOR": "kr",
    "KWT": "kw",
    "CYM": "ky",
    "KAZ": "kz",
    "LAO": "la",
    "LBN": "lb",
    "LCA": "lc",
    "LKA": "lk",
    "LBR": "lr",
    "LTU": "lt",
    "LUX": "lu",
    "LVA": "lv",
    "MDA": "md",
    "MDG": "mg",
    "MKD": "mk",
    "MLI": "ml",
    "MNG": "mn",
    "MAC": "mo",
    "MRT": "mr",
    "MSR": "ms",
    "MLT": "mt",
    "MUS": "mu",
    "MWI": "mw",
    "MEX": "mx",
    "MYS": "my",
    "MOZ": "mz",
    "NAM": "na",
    "NER": "ne",
    "NGA": "ng",
    "NIC": "ni",
    "NLD": "nl",
    "NOR": "no",
    "NPL": "np",
    "NZL": "nz",
    "OMN": "om",
    "PAN": "pa",
    "PER": "pe",
    "PNG": "pg",
    "PHL": "ph",
    "PAK": "pk",
    "POL": "pl",
    "PRT": "pt",
    "PLW": "pw",
    "PRY": "py",
    "QAT": "qa",
    "ROU": "ro",
    "RUS": "ru",
    "SAU": "sa",
    "SLB": "sb",
    "SYC": "sc",
    "SWE": "se",
    "SGP": "sg",
    "SVN": "si",
    "SVK": "sk",
    "SLE": "sl",
    "SEN": "sn",
    "SUR": "sr",
    "STP": "st",
    "SLV": "sv",
    "SWZ": "sz",
    "TCA": "tc",
    "TCD": "td",
    "THA": "th",
    "TJK": "tj",
    "TKM": "tm",
    "TUN": "tn",
    "TUR": "tr",
    "TTO": "tt",
    "TWN": "tw",
    "TZA": "tz",
    "UKR": "ua",
    "UGA": "ug",
    "USA": "us",
    "URY": "uy",
    "UZB": "uz",
    "VCT": "vc",
    "VEN": "ve",
    "VGB": "vg",
    "VNM": "vn",
    "YEM": "ye",
    "ZAF": "za",
    "ZWE": "zw",
}
"""ISO 3166-1 alpha-3 to alpha-2, for every storefront in ``Country``.

App Store Connect reports a review's storefront as alpha-3 (``"USA"``), while the
RSS feed and this enum use alpha-2 (``"us"``). Without a translation the same
field would carry two alphabets depending on which source produced the review.

Hand-written, then checked: all 155 pairs were diffed against CLDR's
``territoryAlias`` table (via ``babel.core.get_global("territory_aliases")``) with
zero mismatches. Redo that diff if entries are added: a wrong pair here is a
silent data bug, and the bijection tests in ``test_country.py`` cannot catch one.
"""


_ALPHA2_CODES: frozenset[str] = frozenset(c.value for c in Country)
"""The alpha-2 values, as plain strings.

``code in Country`` would read better, but it only works on Python 3.12 and up.
Before that, testing a non-member against an enum class raises ``TypeError``, and
this package supports 3.11. A value set is version-independent, and cheaper than
the membership protocol either way.
"""


def normalise_country(code: str | None, *, warn_unknown: bool = True) -> str | None:
    """Return a storefront code in the alpha-2 form ``Country`` uses.

    Accepts either alphabet, in any case, with surrounding whitespace. An
    unrecognised code is returned unchanged rather than dropped (losing a
    storefront is worse than reporting an odd one), and logged so it can be
    added here.

    ``warn_unknown=False`` keeps the normalisation and drops the logging.
    ``Country`` is the iTunes storefront list, so "not a member" means "wrong"
    only for Apple. Google Play serves markets Apple has no storefront in
    (Serbia, Bosnia, Morocco), and warning on those would be noise on a
    correct call.
    """
    if not code or not (code := code.strip()):
        return None

    if len(code) == 2:
        alpha2 = code.lower()
    else:
        mapped = _ALPHA3_TO_ALPHA2.get(code.upper())
        if mapped is None:
            if warn_unknown:
                _LOG.warning("Unrecognised storefront code %r, passing through", code)
            return code
        alpha2 = mapped

    if warn_unknown and alpha2 not in _ALPHA2_CODES:
        _LOG.warning("Unrecognised storefront code %r, passing through", code)
    return alpha2
