# Two-host validation of per-QP tracing, 2026-07-28

Loopback proved the accounting reconciles and costs nothing. It could not
prove the two things that only exist on a real fabric, which is what this
run is for.

## Verdict

**Both validated, positive.** The per-QP bandwidth trace captures contention
dynamics at 50 ms and finer, and the per-QP retransmit counters move and
attribute correctly. Nothing here calls the design into question.

One caveat that limits how far the *numbers* travel, though not the tool:
see "lab state" below.

## Setup

Incast onto one destination VF, with a join and a leave:

```
t=0    A: sgpu01 vf0 (mlx5_6) -> sgpu02 vf0 (10.1.0.2), 4 QPs, 65536 B
t=13   B: sgpu01 vf3 (mlx5_9) -> sgpu02 vf0,            4 QPs, 65536 B
t=43   B leaves
t=60   A ends
```

The two source VFs are picked so they do not share a hardware rate-limiter
budget: on this fabric two of the four VFs hash together, and such a pair
would look throttled for a reason unrelated to what is being tested.

`run.sh` reproduces it; machine names are the ones it was run on.

## (a) Retransmit counters move, and attribute correctly

| flow | packet_seq_err over the run |
|---|---|
| A (incumbent, lqpn 1262-1265) | 0, 0, 0, 0 |
| B (newcomer, lqpn 546-549) | 2, 2, 1, 1 |

All four of B's landed in the **same 100 ms sample, 0.42 s after B joined** -
the collision transient. The incumbent recorded none, all run.

That is exactly the attribution the mechanism promises: the counters
distinguish which flow suffered, and when. It is also a small signal - single
digits, because this fabric is close to lossless - so treat these as an
event marker for a transient, not a rate to plot. Whether they grow to
useful magnitudes under a harsher regime is untested.

## (b) Per-QP curves capture the dynamics

At 1 s (what the old tooling could see) versus 50 ms (what this can):

```
 t      A Gb/s   B Gb/s   total
 16.0    25.7     28.2     53.9    <- both at full rate, 3 s after B joined
 17.4    26.4     27.1     53.5
 17.6    26.0     15.7     41.7    <- step 1: B alone drops, A untouched
 19.0    25.9     10.6     36.5
 19.2    18.7     10.6     29.3    <- step 2: A drops
 19.8    10.6     10.6     21.2    <- settled, even split
```

Three things a 1 s average cannot show, all of which the trace does:

1. **~4.5 s of no congestion response.** Both flows held ~27 Gb/s from B's
   join at t=13 until t=17.5. The receive path absorbed 54 Gb/s for four and
   a half seconds.
2. **The rate-down is two discrete steps, not a ramp** - B alone at t≈17.5,
   then both at t≈19.2-19.8. Sharp steps, not continuous AIMD.
3. **Recovery after B leaves takes 8 s** (t=43 -> t=51 back to 27.8 Gb/s),
   with a dip at t=48 partway up. Not the permanent failure recorded in
   observed on this fabric before, but far from instant.

Also: the settled total is **21.2 Gb/s against A's 27.7 solo** - contention
costs ~24% of aggregate throughput between two senders onto one dst VF.

## What was in the path, and what it means for these numbers

A programmable rate limiter was live on the sender's DPU with nothing
feeding it targets - started by hand, so the service manager reported it
inactive, which is how it went unnoticed at first.

So an executor was in the path doing whatever it does by default. **The
dynamics above are that limiter's, not the NIC's native congestion
control** - the likely explanation for the sharp rate steps and the round
10.6 Gb/s plateau. Do not cite these numbers as DCQCN behaviour.

This does not weaken the validation: congestion was real, the flows
contended, the curves separated, the counters fired. It does mean the
congestion-control observations are a by-product worth re-running
deliberately rather than a result.

## Not established

- Whether retransmit counters reach useful magnitudes under real loss. This
  fabric produced 6 events total.
- Whether the sender-side completion curve tracks the wire during the
  transients. An RC WRITE completes on ACK with `tx_depth` in flight, so the
  two-step rate-down could be shaped by that rather than by the executor.
  Settling this needs the receiver-side vport meter alongside, which this run
  did not collect.
- Anything about the rate limiter itself; it was simply present.

## Files

`results/{A,B}_trace.csv` per-QP event traces (38 MB / 14 MB),
`results/{A,B}_qpstat.csv` per-QP counters at 100 ms,
`results/{A,B}_perftest.log` perftest's own reports.
