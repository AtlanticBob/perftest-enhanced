# Sender completion curve vs receiver wire counters, 2026-07-28

Same incast as `../twohost_incast_20260728`, with the receiver's hardware
vport counters collected alongside. The question left open there: an RC
WRITE completes when its ACK returns with `tx_depth` (128 x 64 KB = 8 MB)
in flight, so the sender's completion curve might lag or smooth the wire.

## Verdict

**Mutual validation succeeded; the lag question did not get answered.**

The two paths agree to 0.07% on volume and track at r = 0.97, which is a
strong result — they share no code, no clock and no host. But the sub-10 ms
timing question is **not answerable with NTP**, and that is a property of
the setup, not of the tools.

## What the two paths are

| | sender | wire |
|---|---|---|
| where | sgpu01, `--report-per-qp` | hpft-dpu2, `QUERY_VPORT_COUNTER` |
| counts | payload bytes on completion | frame octets on arrival |
| clock | sgpu01 TSC, CLOCK_REALTIME at t0 | DPU CLOCK_MONOTONIC + REALTIME |
| cadence | per CQE | 1 ms |

The meter already publishes at 1 ms; only the lab's sampler reads it at
1 Hz. `vpm_sample_fast.py` polls faster than the writer and dedupes on the
published timestamp — 2977 samples in 3 s, i.e. the meter's native rate,
with no change to the meter and no risk to it.

## 1. Volume agreement: 0.07%

Over the whole 60 s run, **wire / sender = 1.0562**.

Independently: the link runs MTU 1024, so a 65536 B write is 64 packets,
each carrying Eth 14 + IP 20 + UDP 8 + BTH 12 + ICRC 4 = 58 B, plus a
16 B RETH on the first. That is 3728 B of headers, ratio **1.0569**.

Predicted 1.0569 against measured 1.0562. Two measurement paths that share
no code, no clock and no host, agreeing to 0.07% once an overhead computed
from the MTU alone is accounted for. Neither path is lying about volume.

## 2. Shape agreement at 5 ms

Correlation of the two series at 5 ms bins over the whole run: **r = 0.97**.

Per-bin variability over a steady stretch is nearly identical — sender
cv 0.195, wire cv 0.196. So the completion curve is **not** smoothing away
structure at the 5 ms scale, which was the specific worry.

## 3. The lag question is not answerable here

Cross-correlating the two series gives r ≈ 0.97 flat across the entire
±30 ms range tested, varying by 0.004 end to end, and drifting toward
*negative* lag — wire leading sender, which is physically impossible for
the same bytes. That drift is residual cross-host clock offset.

So the alignment floor here is tens of milliseconds, against a `tx_depth`
effect of roughly 2.4 ms at 27 Gb/s. **The instrument is 10x coarser than
the effect.** Settling this needs a shared clock — PTP, or CQE hardware
timestamps (`IBV_WC_EX_WITH_COMPLETION_TIMESTAMP`, which perftest does not
currently use) — not more runs of this shape.

## 4. Wire excursions above the sender's rate

At 5 ms bins the wire reaches 78 Gb/s where the sender's payload rate
implies about 59. The link is 100 Gb/s, so this is physically possible and
not a binning artifact. Wire transitions are also broader than sender ones
(595 ms vs 210 ms 10-90% on the largest step).

Most plausible cause: the vport counter counts retransmitted and
out-of-order arrivals that are discarded and never complete, so under
go-back-N a retransmit burst shows on the wire and not at the sender. This
is **unproven** — the per-QP retransmit counters sample at 100 ms and
cannot confirm a 5 ms burst.

## A bug found and fixed in the analysis

The first version of `analyze.py` charged each counter delta to the bin
holding its *end* timestamp. The meter samples at 1 ms but occasionally
runs late, so a 3 ms delta landed entirely in one 5 ms bin — inventing a
spike there and a hole next door, at one point an 84 Gb/s bin. Fixed to
spread each delta across every bin its interval covers.

Anyone re-binning a counter-difference series needs this; it is not
specific to this experiment.

## Not established

- Sub-10 ms lag between completion and wire (see 3).
- Whether the wire excess is retransmission (see 4).
- Anything about HPFT: its agents were off, though `doca_pcc` was live on
  the sender DPU, as in the previous run.

## Files

`results/{A,B}_trace.csv` sender per-QP traces, `results/wire.csv` the 1 ms
vport series (285665 rows, 4 vports), `run.sh`, `analyze.py`.
Data is gitignored; this file carries the findings.
