"""Export the nosms machine-facing contract into the napplet's build tree.

Single source of truth: `app/pricing.py` owns the price table; this script bakes
it into `napplet/src/contract/pricing.json` so the napplet's bundled fallback and
the Python service can never disagree. The CVM server reads the SAME table file
at runtime, so live values and bundled values share one origin.

Usage:  python3 scripts/export_contract.py [--check]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.pricing import (  # noqa: E402
    DEFAULT_PRICE_SATS,
    MIN_E164_LEN,
    PREFIX_PRICES,
)

OUT = REPO / "napplet" / "src" / "contract" / "pricing.json"


def build() -> dict:
    return {
        "_generated_by": "scripts/export_contract.py from app/pricing.py",
        "unit": "sats",
        "prefixes": dict(sorted(PREFIX_PRICES.items(), key=lambda kv: -len(kv[0]))),
        "default": DEFAULT_PRICE_SATS,
        "min_e164_len": MIN_E164_LEN,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed file is stale")
    args = parser.parse_args()

    payload = json.dumps(build(), indent=2) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text() != payload:
            print(f"STALE: {OUT} does not match app/pricing.py", file=sys.stderr)
            return 1
        print(f"OK: {OUT} is current")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(payload)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
