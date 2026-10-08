#!/usr/bin/env python3
"""Find top disk writers/readers on a host over a 5s window (no iotop needed)."""
import os
import time


def snap():
    d = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/io") as f:
                io = f.read()
            with open(f"/proc/{pid}/comm") as f:
                c = f.read().strip()
        except Exception:
            continue
        w = int([l for l in io.splitlines() if l.startswith("write_bytes")][0].split()[1])
        r = int([l for l in io.splitlines() if l.startswith("read_bytes")][0].split()[1])
        d[pid] = (w, r, c)
    return d


a = snap()
time.sleep(5)
b = snap()
rows = []
for pid, (w, r, c) in b.items():
    if pid in a:
        dw = (w - a[pid][0]) / 5 / 1024
        dr = (r - a[pid][1]) / 5 / 1024
        if dw > 100 or dr > 100:
            rows.append((dw + dr, dw, dr, pid, c))
rows.sort(reverse=True)
for t, dw, dr, pid, c in rows[:10]:
    print(f"{c[:24]:26} pid={pid} W={dw:8.0f}KB/s R={dr:8.0f}KB/s")
if not rows:
    print("no process over 100KB/s in window")
