"""Export the napplet's build-time contract artifacts.

Two files, one source of truth each:

* ``napplet/src/contract/pricing.json`` — from ``app/pricing.py``, so the
  napplet's pre-send price and the service's metered price cannot disagree.
* ``napplet/src/contract/server.json`` — the default ContextVM server to use
  when discovery finds nothing (server npub is only known after the server
  first runs, so this is generated, not hand-written).

Usage:  python3 scripts/export_contract.py [--check] [--pubkey <npub|hex>]
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
    RAIL_REPLACEMENT_USD,
    RISK_MULTIPLIER,
    sats_per_usd_from_btcusd,
)

CONTRACT = REPO / "napplet" / "src" / "contract"
PRICING_OUT = CONTRACT / "pricing.json"
SERVER_OUT = CONTRACT / "server.json"
KEY_FILE = REPO / ".cvm-server.nsec"
DEFAULT_RELAYS = ["wss://relay.primal.net"]

#: BTCUSD quote the shipped default was computed at (ADR-0002, 2026-10-05).
BTCUSD_AT_ADR = 86462


def pricing_payload() -> dict:
    """ADR-0002 flat pricing. No per-prefix table: the rail's cost has none.

    Kept in step with ``CvmTools.sms_pricing`` and ``CvmTools.capability_tags`` —
    all three read ``app/pricing.py``, so the napplet's pre-send price, the CEP-8
    cap tag and the server's metered price cannot disagree (CEP draft 0001 P4).
    """
    return {
        "_generated_by": "scripts/export_contract.py from app/pricing.py",
        "unit": "sats",
        "model": "flat",
        "default": DEFAULT_PRICE_SATS,
        "min_e164_len": MIN_E164_LEN,
        "formula": "ceil(MULT * rail_replacement_usd * sats_per_usd), rounded up to 100",
        "risk_multiplier": RISK_MULTIPLIER,
        "rail_replacement_usd": RAIL_REPLACEMENT_USD,
        "_btcusd_at_default": BTCUSD_AT_ADR,
        "_sats_per_usd_at_default": round(sats_per_usd_from_btcusd(BTCUSD_AT_ADR), 2),
    }


def pubkey_from_key_file(key_file: pathlib.Path = KEY_FILE) -> str | None:
    """Derive the server npub from the local server key, without printing it.

    ``nostr_sdk`` is a build-time convenience only (its absence just means the
    default-server field stays empty and the napplet falls back to discovery),
    so a missing module is reported as a note, never a crash.
    """
    if not key_file.exists():
        return None
    try:
        from nostr_sdk import Keys
        return Keys.parse(key_file.read_text().strip()).public_key().to_bech32()
    except ImportError:
        print("NOTE: nostr_sdk is not installed for this interpreter — run "
              "`python3.13 scripts/export_contract.py` (it lives in the 3.13 "
              "user site-packages) or pass --pubkey explicitly.", file=sys.stderr)
        return None
    except Exception:                                            # noqa: BLE001
        return None


def server_payload(pubkey: str | None) -> dict:
    return {
        "_generated_by": "scripts/export_contract.py",
        "_note": "Default ContextVM server used when discovery finds nothing.",
        "pubkey": pubkey or "",
        "relays": DEFAULT_RELAYS,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if a committed artifact is stale")
    ap.add_argument("--pubkey", default=None, help="server npub (default: from key file)")
    ap.add_argument("--key-file", default=str(KEY_FILE),
                    help="server key file to derive the npub from")
    args = ap.parse_args()

    pubkey = args.pubkey or pubkey_from_key_file(pathlib.Path(args.key_file))
    artifacts = {PRICING_OUT: pricing_payload(), SERVER_OUT: server_payload(pubkey)}

    if args.check:
        stale = []
        for path, payload in artifacts.items():
            text = json.dumps(payload, indent=2) + "\n"
            if not path.exists() or path.read_text() != text:
                stale.append(str(path))
        if stale:
            print("STALE: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("OK: contract artifacts are current")
        return 0

    for path, payload in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {path}")
    if not pubkey:
        print("NOTE: no server pubkey yet — the napplet will rely on discovery "
              "until the server has run once and export_contract.py is re-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
