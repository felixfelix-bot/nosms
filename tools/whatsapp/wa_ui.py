#!/usr/bin/env python3
"""Minimal uiautomator-driven control surface for the wa-dev emulator.

Shape (b) of the E2 handoff: this client talks to the adb server running ON
DQ05 over an ssh port-forward (ADB_SERVER_SOCKET=tcp:127.0.0.1:15037), so the
whole WhatsApp-registration loop — OTP read + `input tap/text` — runs locally
on CobradorWave while the emulator stays headless on DQ05.

Reading  : `uiautomator dump` + XML parse (stable, no root needed)
Sending  : `input tap`, `input text`, `input keyevent`
Both are the surfaces the E3 lifecycle card is told to standardise on.

Usage:
  wa_ui.py dump [out.xml]              -> dump + print readable nodes
  wa_ui.py tap-id <resource-id>        -> tap centre of resource-id
  wa_ui.py tap-text <text>             -> tap first node whose text/desc matches
  wa_ui.py type <string>               -> focus-free `input text` (spaces escaped)
  wa_ui.py key <KEYCODE>               -> keyevent (e.g. ENTER, BACK, DEL)
  wa_ui.py text <resource-id> <string> -> focus field, clear, type
  wa_ui.py wait <resource-id> [secs]   -> poll until node appears
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADB = os.environ.get("ADB", os.path.join(HERE, "adb37"))
SERIAL = os.environ.get("SERIAL", "emulator-5554")
ENV = dict(os.environ, ADB_SERVER_SOCKET=os.environ.get(
    "ADB_SERVER_SOCKET", "tcp:127.0.0.1:15037"))


def adb(*args: str, timeout: int = 60, binary: bool = False):
    p = subprocess.run([ADB, "-s", SERIAL, *args], env=ENV,
                       capture_output=True, timeout=timeout)
    out = p.stdout if binary else p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


def dump_xml(path: str = "/sdcard/wa_ui.xml") -> str:
    adb("shell", f"uiautomator dump {path}", timeout=60)
    _, out, _ = adb("shell", f"cat {path}", timeout=30)
    return out


def nodes(xml: str):
    for m in re.finditer(r"<node[^>]*?/?>", xml):
        s = m.group(0)
        d = {}
        for k in ("text", "content-desc", "class", "resource-id", "clickable",
                  "bounds", "enabled", "focusable", "focused", "password"):
            r = re.search(k + r'="([^"]*)"', s)
            d[k] = r.group(1) if r else ""
        for k in ("text", "content-desc"):
            d[k] = d[k].replace("&#10;", "\n").replace("&amp;", "&").replace("&quot;", '"')
        d["_tag"] = s
        yield d


def center(bounds: str):
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def find(xml: str, *, rid: str | None = None, text: str | None = None,
         desc: str | None = None, exact: bool = False):
    for n in nodes(xml):
        if rid and n["resource-id"] != rid:
            continue
        if text is not None:
            if (n["text"] == text) if exact else (text.lower() in n["text"].lower()):
                return n
            continue
        if desc is not None:
            if (n["content-desc"] == desc) if exact else (desc.lower() in n["content-desc"].lower()):
                return n
            continue
        return n
    return None


def tap(x: int, y: int):
    return adb("shell", f"input tap {x} {y}")


def print_nodes(xml: str):
    for n in nodes(xml):
        if not (n["text"] or n["content-desc"]):
            continue
        c = n["class"].split(".")[-1]
        ctr = center(n["bounds"])
        print(f"[{c}] id={n['resource-id'] or '-'} click={n['clickable']} "
              f"en={n['enabled']} ctr={ctr} | text={n['text']!r} desc={n['content-desc']!r}")


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    cmd = a[0]
    if cmd == "dump":
        xml = dump_xml()
        if len(a) > 1 and a[1] != "-":
            open(a[1], "w").write(xml)
        print_nodes(xml)
        return 0
    xml = dump_xml()
    if cmd == "tap-id":
        n = find(xml, rid=a[1]) if len(a) > 1 else None
        if not n:
            print(f"NOT FOUND rid={a[1]}"); print_nodes(xml); return 3
        x, y = center(n["bounds"])
        print(f"tapping rid={a[1]} at {x},{y}")
        tap(x, y); return 0
    if cmd == "tap-text":
        n = find(xml, text=a[1]) or find(xml, desc=a[1])
        if not n:
            print(f"NOT FOUND text={a[1]}"); print_nodes(xml); return 3
        x, y = center(n["bounds"])
        print(f"tapping text={a[1]!r} at {x},{y}")
        tap(x, y); return 0
    if cmd == "tap-desc":
        n = find(xml, desc=a[1])
        if not n:
            print(f"NOT FOUND desc={a[1]}"); print_nodes(xml); return 3
        x, y = center(n["bounds"])
        print(f"tapping desc={a[1]!r} at {x},{y}")
        tap(x, y); return 0
    if cmd == "type":
        s = a[1].replace(" ", "%s")
        return adb("shell", f"input text {s}")[0]
    if cmd == "key":
        return adb("shell", f"input keyevent {a[1]}")[0]
    if cmd == "text":
        rid, val = a[1], a[2]
        n = find(xml, rid=rid)
        if not n:
            print(f"NOT FOUND rid={rid}"); print_nodes(xml); return 3
        x, y = center(n["bounds"])
        tap(x, y); time.sleep(1.0)
        # clear: select-all + delete
        adb("shell", f"input keyevent KEYCODE_MOVE_END")
        for _ in range(len(val) + 8):
            adb("shell", "input keyevent 67")
        adb("shell", f"input text {val.replace(' ', '%s')}")
        return 0
    if cmd == "wait":
        rid = a[1]; secs = int(a[2]) if len(a) > 2 else 30
        t0 = time.time()
        while time.time() - t0 < secs:
            xml = dump_xml()
            if find(xml, rid=rid):
                print(f"FOUND {rid} after {int(time.time()-t0)}s"); return 0
            time.sleep(2)
        print(f"TIMEOUT waiting for {rid}")
        print_nodes(xml)
        return 4
    print(f"unknown command {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
