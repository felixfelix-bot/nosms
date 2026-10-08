#!/usr/bin/env python3
"""Trim the official NIP-44 vectors down to the parts this repo tests.

The upstream file (github.com/paulmillr/nip44, the vectors the NIP-44 spec
points at) also carries `encrypt_decrypt_long_msg` whose entries are megabytes
of plaintext; those are dropped here so the fixture stays reviewable. Everything
kept is byte-identical to upstream.
"""
import json
import pathlib

SRC = pathlib.Path("/tmp/nip44.vectors.json")
DST = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "nip44.vectors.json"

src = json.loads(SRC.read_text())
v2 = src["v2"]
out = {
    "source": "https://raw.githubusercontent.com/paulmillr/nip44/main/nip44.vectors.json",
    "note": "trimmed: encrypt_decrypt_long_msg removed (megabyte plaintexts)",
    "v2": {
        "valid": {
            "get_conversation_key": v2["valid"]["get_conversation_key"],
            "get_message_keys": v2["valid"]["get_message_keys"],
            "calc_padded_len": v2["valid"]["calc_padded_len"],
            "encrypt_decrypt": v2["valid"]["encrypt_decrypt"],
        },
        "invalid": v2["invalid"],
    },
}
DST.parent.mkdir(parents=True, exist_ok=True)
DST.write_text(json.dumps(out, indent=1) + "\n")
print(f"wrote {DST} {DST.stat().st_size} bytes")
print("conversation_key vectors:", len(out["v2"]["valid"]["get_conversation_key"]))
print("encrypt_decrypt vectors:", len(out["v2"]["valid"]["encrypt_decrypt"]))
print("calc_padded_len vectors:", len(out["v2"]["valid"]["calc_padded_len"]))
print("invalid.decrypt:", len(out["v2"]["invalid"]["decrypt"]))
print("invalid.get_conversation_key:", len(out["v2"]["invalid"]["get_conversation_key"]))
print("invalid.encrypt_msg_lengths:", out["v2"]["invalid"]["encrypt_msg_lengths"])
