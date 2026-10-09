#!/usr/bin/env python3
"""Bounded diagnostic: click NEXT then dump the hierarchy 6x at ~1s intervals.

Runs ON dq05 (see u2_probe.py rationale). Prints one JSON per line:
  {"i": N, "ts": ..., "texts": [...visible texts/descs...]}
Catches transient spinners/dialogs that a single post-hoc dump misses.
"""
import json
import time

import uiautomator2 as u2

d = u2.connect("emulator-5554")
d(resourceId="com.whatsapp:id/button_view").click(timeout=15)
t0 = time.time()
for i in range(6):
    time.sleep(max(0.0, t0 + i - time.time()))
    try:
        xml = d.dump_hierarchy()
    except Exception as e:  # noqa: BLE001 — diagnostic, keep going
        print(json.dumps({"i": i, "err": str(e)[:120]}))
        continue
    import re
    texts = []
    for m in re.finditer(r'<node[^>]*?>', xml):
        s = m.group(0)
        t = re.search(r'text="([^"]*)"', s)
        c = re.search(r'content-desc="([^"]*)"', s)
        r = re.search(r'resource-id="([^"]*)"', s)
        val = (t.group(1) if t else "") or (c.group(1) if c else "")
        if val and "systemui" not in (r.group(1) if r else ""):
            texts.append(val[:48])
    print(json.dumps({"i": i, "t": round(time.time() - t0, 1), "texts": texts[:14]}))
