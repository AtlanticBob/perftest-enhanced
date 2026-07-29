#!/usr/bin/env python3
"""Overlay the sender's completion curve on the receiver's wire counters.

Sender side: --report-per-qp events, payload bytes, stamped in sgpu01's TSC
with CLOCK_REALTIME captured at t0. Wire side: QUERY_VPORT_COUNTER octets
for the destination VF, stamped in the DPU's CLOCK_MONOTONIC with
CLOCK_REALTIME sampled alongside. Both are mapped onto CLOCK_REALTIME and
binned identically.

The wire figure reads a few percent above the sender's: rx_ib counts frame
octets and the sender counts payload. For 65536 B writes at 4096 MTU that
is ~1.5% of headers, so a persistent gap of that order is correct and a
much larger one is not.
"""
import collections
import os
import re
import sys

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
BIN_MS = float(sys.argv[1]) if len(sys.argv) > 1 else 50.0
DST_VPORT_IDX = 0                       # vport 1 = pf1vf0 = the incast dst


def sender_series(path, bin_ns):
    """realtime bin -> payload bytes completed."""
    head = open(path).read(4000)
    t0 = int(re.search(r"t0_realtime_ns=(\d+)", head).group(1))
    size = int(re.search(r"msg_size=(\d+)", head).group(1))
    out = collections.Counter()
    for ln in open(path):
        if ln[0] in "#t":
            continue
        t_us, _q, n = ln.split(",")
        out[int((t0 + float(t_us) * 1000) // bin_ns)] += int(n) * size
    return out


def wire_series(path, bin_ns):
    """realtime bin -> frame octets arriving at the destination VF.

    Intervals come from the meter's own monotonic stamp (t_ns), which is
    what the counters are actually spaced by; the sampler's realtime is
    used only to place that series on the shared clock.
    """
    rows = []
    for ln in open(path):
        if ln.startswith("#") or ln.startswith("real"):
            continue
        f = ln.split(",")
        if int(f[1]) != DST_VPORT_IDX:
            continue
        rows.append((int(f[0]), int(f[2]), int(f[3])))
    if len(rows) < 2:
        sys.exit("no wire samples for vport index %d" % DST_VPORT_IDX)
    off = rows[0][0] - rows[0][1]       # realtime - monotonic, fixed per host
    out = collections.Counter()
    for i in range(1, len(rows)):
        t_a, t_b = rows[i - 1][1] + off, rows[i][1] + off
        dt = t_b - t_a
        db = rows[i][2] - rows[i - 1][2]
        if dt <= 0 or db < 0:           # counter wrap or a stalled writer
            continue
        # Spread the delta across every bin its interval covers, rather
        # than charging it all to the bin holding its end timestamp. The
        # meter samples at 1 ms but occasionally runs late; charging a
        # 3 ms delta to one 5 ms bin invents a spike there and a hole next
        # door. It showed up as an 84 Gb/s bin on a link that cannot do it.
        k_a, k_b = int(t_a // bin_ns), int(t_b // bin_ns)
        if k_a == k_b:
            out[k_a] += db
            continue
        for k in range(k_a, k_b + 1):
            lo = max(t_a, k * bin_ns)
            hi = min(t_b, (k + 1) * bin_ns)
            if hi > lo:
                out[k] += db * (hi - lo) / dt
    return out


def main():
    bin_ns = int(BIN_MS * 1e6)
    snd = collections.Counter()
    for tag in ("A", "B"):
        p = os.path.join(BASE, "%s_trace.csv" % tag)
        if os.path.exists(p):
            snd.update(sender_series(p, bin_ns))
    wire = wire_series(os.path.join(BASE, "wire.csv"), bin_ns)

    keys = [k for k in sorted(set(snd) | set(wire))]
    # trim to where the sender was actually running
    active = [k for k in keys if snd.get(k, 0) > 0]
    keys = [k for k in keys if active[0] <= k <= active[-1]]
    sec = BIN_MS / 1000.0
    gb = lambda c, k: c.get(k, 0) * 8 / sec / 1e9

    print("bin=%.0f ms  bins=%d  (sender payload vs wire frame octets)"
          % (BIN_MS, len(keys)))
    tot_s = sum(snd.get(k, 0) for k in keys)
    tot_w = sum(wire.get(k, 0) for k in keys)
    print("  totals: sender %.2f GB  wire %.2f GB  wire/sender %.4f"
          % (tot_s / 1e9, tot_w / 1e9, tot_w / tot_s if tot_s else 0))
    print("  t_s     sender   wire    w/s")
    t0 = keys[0]
    for k in keys:
        s, w = gb(snd, k), gb(wire, k)
        print("  %6.2f  %6.1f  %6.1f  %5.3f"
              % ((k - t0) * sec, s, w, w / s if s > 0.1 else 0))


if __name__ == "__main__":
    main()
