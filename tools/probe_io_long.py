#!/usr/bin/env python3
"""Probe 2: longer window (10s), lower threshold (30KB/s), include children."""
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
            with open(f"/proc/{pid}/stat") as f:
                st = f.read().split()
            state = st[2]
        except Exception:
            continue
        w = int([l for l in io.splitlines() if l.startswith("write_bytes")][0].split()[1])
        r = int([l for l in io.splitlines() if l.startswith("read_bytes")][0].split()[1])
        cw = int([l for l in io.splitlines() if l.startswith("cancelled_write_bytes")][0].split()[1])
        d[pid] = (w, r, cw, c, state)
    return d


a = snap()
time.sleep(10)
b = snap()
rows = []
for pid, (w, r, cw, c, s) in b.items():
    if pid in a:
        dw = (w - a[pid][0]) / 10 / 1024
        dr = (r - a[pid][1]) / 10 / 1024
        dcw = (cw - a[pid][2]) / 10 / 1024
        if dw > 30 or dr > 30 or dcw > 30:
            rows.append((dw + dr + dcw, dw, dr, dcw, pid, c, s))
rows.sort(reverse=True)
for t, dw, dr, dcw, pid, c, s in rows[:12]:
    print(f"{c[:22]:24} pid={pid} state={s} W={dw:7.0f} R={dr:7.0f} cW={dcw:7.0f} KB/s")
if not rows:
    print("nothing over 30KB/s in 10s window")
