"""NANP country gate shared by every rail.

``countries`` advertises exactly ``["US", "CA"]`` on both rails, so the gate must
mean *US or Canada*, not "any +1". All of the following are ``+1`` with a
valid NPA and are **not** US/CA — the Caribbean members of the NANP and the US
territories:

* Caribbean: 242 Bahamas, 246 Barbados, 264 Anguilla, 268 Antigua, 284 BVI,
  345 Cayman, 441 Bermuda, 473 Grenada, 649 Turks & Caicos, 658/876 Jamaica,
  664 Montserrat, 721 Sint Maarten, 758 St Lucia, 767 Dominica, 784 St Vincent,
  809/829/849 Dominican Republic, 868 Trinidad, 869 St Kitts
* US territories (US *numbering* is not the same as being US or Canada for a
  message rail): 340 USVI, 670 CNMI, 671 Guam, 684 American Samoa, 787/939
  Puerto Rico

The rule is therefore an explicit *blocked* set rather than an area-digit
heuristic: ``787`` (Puerto Rico) and ``340`` (USVI) start with 7 and 3, so any
"US/CA area codes start 2-9" rule lets them straight through. A curated set is
the only honest gate without shipping a full NPA database; it is tested.

A full NPA table (and an HLR lookup) is the real answer and is a later milestone.
"""
from __future__ import annotations

__all__ = ["NANP_NON_US_CA", "is_us_ca"]

#: NANP area codes that are NOT the US or Canada.
NANP_NON_US_CA: frozenset[str] = frozenset({
    "242", "246", "264", "268", "284", "340", "345", "441", "473", "649",
    "658", "664", "670", "671", "684", "721", "758", "767", "784", "787",
    "809", "829", "849", "868", "869", "876", "939",
})


def is_us_ca(number: str) -> bool:
    """True for a US or Canada E.164 number (``+1`` NPA-NXX-XXXX).

    ``number`` must already be normalised to ``+<digits>``. A ``+1`` number whose
    NPA is a Caribbean or US-territory code is refused: neither is US or Canada.
    """
    if not number.startswith("+1") or len(number) != 12:
        return False
    return number[2:5] not in NANP_NON_US_CA
