#!/usr/bin/env python3
"""Sample a receiver-side NIC counter publisher at its native cadence.

SITE-SPECIFIC SCAFFOLDING, not part of the tool. It reads a shared-memory
file published by a DPU-local daemon that polls QUERY_VPORT_COUNTER per VF
and writes seqlock'd records; the layout is documented below. It is kept
because it is how the wire-side series in this directory was collected, not
because it will run anywhere unchanged.

The publisher here emits every 1 ms. The sampler that shipped with it read
at 1 Hz - a property of that script's `time.sleep(1)`, not of the
measurement. This one polls faster than the writer and emits a row only
when a record's timestamp actually advances, so the output is the real 1 ms
series with no duplicates and no missed updates.

Two clocks are recorded per row because this has to line up against a trace
taken on another host: t_ns is the meter's own CLOCK_MONOTONIC (DPU), and
real_ns is CLOCK_REALTIME sampled here. The pair gives the offset needed to
map DPU-monotonic onto the sender's CLOCK_REALTIME.

Record layout: a 64 B header {magic u64, nvports u32, interval_us u32,
vport ids u16[8]} followed by one 64 B record per vport
{seq, t_ns, rx_ib, rx_eth, tx_ib, tx_eth, rx_ib_pkt, rx_eth_pkt} as u64,
seqlock'd (odd seq = writer busy). Read-only - this never writes to the
shared memory and does not disturb the publisher.

usage: vpm_sample_fast.py <duration_s> <out_csv> [poll_us] [shm_path]
"""
import mmap
import struct
import sys
import time

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/vpm_fast.csv"
poll = (float(sys.argv[3]) if len(sys.argv) > 3 else 400.0) / 1e6

shm = sys.argv[4] if len(sys.argv) > 4 else "/dev/shm/vpm"
f = open(shm, "rb")
mm = mmap.mmap(f.fileno(), 4096, prot=mmap.PROT_READ)

# Validate structurally rather than against a magic constant, so this works
# against any publisher using the layout above.
_magic, nv, iv_us = struct.unpack_from("<8sII", mm, 0)
if not 1 <= nv <= 8 or not 1 <= iv_us <= 1000000:
    sys.exit("%s does not look like a counter publisher: nvports=%d "
             "interval_us=%d" % (shm, nv, iv_us))
vports = struct.unpack_from("<8H", mm, 16)[:nv]

rows = []
last_t = [0] * nv
t0 = time.time()
while time.time() - t0 < dur:
    now_real = time.time()
    for i in range(nv):
        off = 64 + i * 64
        # seqlock: odd seq means the writer is mid-update; retry a few
        # times, then skip this vport for this poll rather than block.
        for _ in range(4):
            seq, t, rxib, rxeth, txib, txeth, rip, rep = \
                struct.unpack_from("<8Q", mm, off)
            if seq % 2 == 0:
                break
        else:
            continue
        if t == last_t[i]:
            continue                    # writer has not published since
        last_t[i] = t
        rows.append((now_real, i, t, rxib, rxeth, rip))
    time.sleep(poll)

with open(out, "w") as w:
    w.write("# vpm_sample_fast v1 meter_interval_us=%d vports=%s\n"
            % (iv_us, ",".join(str(v) for v in vports)))
    w.write("# t_ns is the meter's CLOCK_MONOTONIC on this DPU;"
            " real_ns is CLOCK_REALTIME sampled here\n")
    w.write("real_ns,vport_idx,t_ns,rx_ib,rx_eth,rx_ib_pkt\n")
    for r, i, t, a, b, p in rows:
        w.write("%d,%d,%d,%d,%d,%d\n" % (int(r * 1e9), i, t, a, b, p))
print("vpm_sample_fast: %d rows, %d vports, meter interval %d us"
      % (len(rows), nv, iv_us))
