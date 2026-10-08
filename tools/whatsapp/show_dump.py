#!/usr/bin/env python3
"""Print readable nodes from a u2_probe dump JSON (stdin) — local helper."""
import json
import re
import sys

blob = json.load(sys.stdin)
xml = blob.get("xml", "")
out = blob.get("out_path")
if out:
    open(out, "w").write(xml)
for m in re.finditer(r"<node[^>]*?>", xml):
    s = m.group(0)

    def attr(k):
        r = re.search(k + r'="([^"]*)"', s)
        return r.group(1) if r else ""
    t, c, rid, cl = attr("text"), attr("content-desc"), attr("resource-id"), attr("class")
    if t or c:
        print(f"[{cl.split('.')[-1] or '?'}] rid={rid or '-'} | "
              f"text={t!r} desc={c!r}")
