#!/usr/bin/env python3
"""T+15 min auto-refund sweep — the timer body.

Refunds every message that was paid for and is still not delivered inside the
configured window. Idempotent by construction: each refund is claimed with a
single conditional UPDATE in `EscrowStore`, so overlapping runs (a timer firing
while the previous run is still working) cannot pay out twice.

Runs against the same env the service reads:
    NOSMS_DB_PATH, NOSMS_MINT_URL, NOSMS_TRANSPORT, NOSMS_REFUND_AFTER_SECONDS,
    NOSMS_SMS_GATEWAY_PATH.

Exit 0 when the sweep ran (even if it refunded nothing). Exit 1 only when the
sweep itself could not run, so a systemd unit surfaces a real failure instead of
looking healthy. Prints one JSON line; `systemd-cat`/journald keeps the history.

    python scripts/refund_sweep.py            # uses env config
    python scripts/refund_sweep.py --dry-run  # report only, claim nothing
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from app.escrow import EscrowStore  # noqa: E402
from app.main import build_transport  # noqa: E402
from app.refunds import sweep_refunds  # noqa: E402


def run(cfg: Config | None = None, dry_run: bool = False) -> dict:
    cfg = cfg or Config.from_env()
    store = EscrowStore(cfg.escrow_db)
    transport = build_transport(cfg)
    try:
        if dry_run:
            pending = [r for r in store.list_unrefunded()
                       if not r.refunded and r.created_at is not None]
            return {"dry_run": True, "candidates": len(pending),
                    "refund_after_seconds": cfg.refund_after_seconds,
                    "transport": cfg.transport}
        out = sweep_refunds(store, transport,
                            refund_after_seconds=cfg.refund_after_seconds)
        out.update({"transport": cfg.transport, "mint": cfg.mint_url,
                    "refund_after_seconds": cfg.refund_after_seconds})
        return out
    finally:
        store.close()


def main(argv: list[str]) -> int:
    try:
        report = run(dry_run="--dry-run" in argv)
    except Exception as exc:                                     # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    report["ok"] = True
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
