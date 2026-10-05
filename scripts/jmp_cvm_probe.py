#!/usr/bin/env python3
"""Real, offline run of the JMP CVM tools — the acceptance evidence.

Runs exactly the path the CVM server runs (CvmTools.call -> app/jmp_tools.py),
with no live XMPP drive and no secrets in the output, and prints one JSON
document:

* ``jmp.status`` — the served funding payload, ``source`` included;
* ``jmp.credentials`` — the *precondition report* only (which store was asked,
  which secret, present or not) plus the refusal reason. It never prints a value;
* whether a live driver is wired, so a caller can see why ``jmp.funding`` would
  refuse on this box.

    python3 scripts/jmp_cvm_probe.py            # print to stdout
    python3 scripts/jmp_cvm_probe.py --out FILE # also write the evidence file
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.cvm_tools import CvmTools                                     # noqa: E402
from app.jmp_secrets import OperatorAllowList                          # noqa: E402
from app.jmp_tools import (                                            # noqa: E402
    TOOL_JMP_CREDENTIALS,
    TOOL_JMP_FUNDING,
    TOOL_JMP_STATUS,
)
from app.transports import FakeTransport                               # noqa: E402

CONTRACT_DIR = REPO / "docs" / "cvm"


def load_docs() -> dict[str, str]:
    return {"llms": (CONTRACT_DIR / "llms.txt").read_text(),
            "llms-full": (CONTRACT_DIR / "llms-full.txt").read_text()}


def body(result: dict) -> dict:
    try:
        return json.loads(result["content"][0]["text"])
    except Exception:                                                  # noqa: BLE001
        return {"unparseable": True, "raw_type": type(result).__name__}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="also write the evidence JSON here")
    ap.add_argument("--caller", default="", help="npub/hex the probe calls as")
    args = ap.parse_args(argv)

    from scripts.jmp_live import build_jmp_service

    # No CVM_NSEC here: the encryption key only matters for jmp.credentials, and
    # the probe's job is to show the *precondition* state, not to release anything.
    jmp = build_jmp_service(server_secret_hex=None,
                            transcript_path=None)
    tools = CvmTools(FakeTransport(), docs=load_docs(), jmp=jmp)

    status = tools.call(TOOL_JMP_STATUS, {})
    funding = tools.call(TOOL_JMP_FUNDING, {})
    credentials = tools.call(TOOL_JMP_CREDENTIALS, {}, caller=args.caller)

    report = {
        "probe": "scripts/jmp_cvm_probe.py",
        "note": ("Real run of the served tool path. No live XMPP drive was "
                 "performed here (that is jmp.funding, which refuses when no "
                 "credential is present), and no secret appears in this file."),
        "jmp.status": {"isError": status.get("isError", False), "payload": body(status)},
        "jmp.funding": {"isError": funding.get("isError", False), "payload": body(funding)},
        "jmp.credentials": {
            "isError": credentials.get("isError", False),
            "refusal": body(credentials),
            "preconditions": jmp.preconditions(),
            "live_driver_wired": jmp.live_driver is not None,
            "status_probe_wired": jmp.status_probe is not None,
            "allow_list_size": len(jmp.allow_list.pubkeys),
        },
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.out:
        path = pathlib.Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
        print(f"\n[probe] wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
