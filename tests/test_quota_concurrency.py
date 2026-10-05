"""RED-first regression for the quota check-then-record TOCTOU race (review t_119a4ab8).

The review found that POST /api/send calls ``quota.check`` and ``quota.record`` as two
separate statements with the awaited escrow swap in between, so two concurrent
same-identity requests to the SAME destination both pass the cooldown: the check-then-act
window is non-atomic even though an atomic, lock-guarded ``check_and_record`` exists on
the store. This test forces that interleave and must FAIL until main.py uses the atomic
call.
"""
from __future__ import annotations

import json
import threading
from uuid import uuid4

from fastapi.testclient import TestClient

from app.pricing import DEFAULT_PRICE_SATS
from tests.conftest import BASE_URL, build_event, nip98_header
from tests.stubs import MINT_URL, StubMint, make_app, make_token

SEND_URL = f"{BASE_URL}/api/send"
DEST_US = "+155****4567"
PRICE = DEFAULT_PRICE_SATS


class BarrierMint(StubMint):
    """Hold the escrow swap at a barrier until BOTH concurrent sends are inside it.

    That is the exact interleave the race needs: each request has already passed
    ``quota.check`` (no record yet) and is parked inside the awaited swap before either
    reaches ``quota.record``. With the fix, the second request is rejected at the
    cooldown BEFORE it ever reaches the swap, so its thread never arrives at the
    barrier and we must not deadlock — hence a bounded wait.
    """

    def __init__(self, *a, arrivals=2, **kw):
        super().__init__(*a, **kw)
        self._bar = threading.Barrier(arrivals, timeout=5)
        self.inside = 0
        self._inside_lock = threading.Lock()

    def swap(self, inputs, output_amounts):
        with self._inside_lock:
            self.inside += 1
        try:
            # wait only if another swap is still expected; a cooldown-rejected second
            # request never swaps, so the first must not block on it forever
            self._bar.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return super().swap(inputs, output_amounts)


def _send(client, token, sk_hex, results, key):
    body = json.dumps({"to": DEST_US, "text": "race probe"}).encode()
    headers = {
        "Authorization": nip98_header(url=SEND_URL, method="POST", body=body,
                                      sk_hex=sk_hex, extra_tags=[["nonce", uuid4().hex]]),
        "Content-Type": "application/json",
        "X-Cashu": token,
    }
    r = client.post(SEND_URL, content=body, headers=headers)
    results[key] = r.status_code


def test_concurrent_same_identity_same_destination_cooldown_is_atomic(tmp_path):
    mint = BarrierMint(fee_ppk=0, arrivals=2)
    app = make_app(tmp_path, transport=None, mint=mint)
    # one shared identity => same pubkey for both requests
    sk = "cd" * 32
    client = TestClient(app)

    # two independently-funded tokens, both large enough for the flat price
    t1 = make_token([PRICE + 64])
    t2 = make_token([PRICE + 64])

    results: dict[str, int] = {}
    th1 = threading.Thread(target=_send, args=(client, t1, sk, results, "a"))
    th2 = threading.Thread(target=_send, args=(client, t2, sk, results, "b"))
    th1.start(); th2.start()
    th1.join(timeout=20); th2.join(timeout=20)

    codes = sorted(results.values())
    # Exactly one send may be accepted; the other MUST be refused by the 60s
    # per-destination cooldown (429 / destination_cooldown). With the race both
    # threads are inside the swap before either records, so the store sees zero
    # records for both checks -> both return 200. The fix makes the second 429.
    assert codes.count(200) == 1, (
        f"cooldown must reject the second concurrent send to the same destination; "
        f"got statuses {codes}")
    assert codes.count(429) == 1, (
        f"expected one 429 destination_cooldown, got statuses {codes}")
    # and the rail must only ever see ONE message
    assert len(mint.swap_calls) == 1
