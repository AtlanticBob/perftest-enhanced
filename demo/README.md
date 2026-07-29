# Demo: the whole workflow, end to end

Run `./run_demo.sh` (~65 s), then `python3 plot_demo.py`. That is the demo.
This file explains what it does, what changes versus stock perftest, what
comes out, and how to read it.

![demo](demo.png)

---

## 1. The experiment

One RDMA flow, joined partway through by a second one aimed at the same
destination VF, then left alone again — the five phases the figure has to
show.

```
t=0     A: sgpu01 vf0 (mlx5_6) -> sgpu02 vf0 (10.1.0.2), 4 QPs, 65536 B, -D 60
t=24.9  B: sgpu01 vf3 (mlx5_9) -> sgpu02 vf0,            4 QPs, 65536 B, -D 20
t=44.5  B leaves
t=59.6  A ends
```

B is held back until t=25 on purpose — see section 6.

`vf0` and `vf3` as the two sources deliberately: `vf1` and `vf2` hash to the
same PCC flowtag for every destination, so that pair would share one budget
for a reason unrelated to the demo.

Nothing in the hpft repos is read or written.

## 2. How the tool is used — the entire difference

Stock invocation, as the lab scripts use it today:

```sh
ib_write_bw -d mlx5_6 -p 18970 -s 65536 -q 4 --report_gbits -D 60 10.1.0.2
```

The demo's invocation:

```sh
ib_write_bw -d mlx5_6 -p 18970 -s 65536 -q 4 --report_gbits -D 60 \
            --report-per-qp --report-csv=results/A_trace.csv 10.1.0.2
#           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

**That is the whole difference.** Two flags. Everything else — the server
side, the ports, `-q`, `-D`, the report perftest prints at the end — is
unchanged, and the stock binary still works as the server.

Two more flags exist and are not used here:

- `--report-interval-us=<us>` switches from one record per completion to
  periodic snapshots. Only for message rates where per-CQE records will not
  fit in memory; it costs you the ability to re-bin offline.
- `--report-trace-mb=<MB>` sizes the buffer (default 256).

One gotcha the demo does not hit but you will: **`-D` is a negotiated
parameter.** The server's duration must equal the client's exactly, or the
handshake dies with `duration mismatch`. Nothing to do with this fork; it
bites as soon as you script two flows with different durations.

Optionally, alongside the run:

```sh
sudo tools/qpstat.py --dev mlx5_6/1 --pid $APID --interval-ms 100 \
                     --duration 57 --out results/A_qpstat.csv
```

Binds a dedicated hardware counter to each of that process's QPs, samples
the retransmit/error set, and unbinds on exit.

## 3. What comes out

```
results/
  A_trace.csv      2.54M events, 41 MB   per-QP completion trace
  B_trace.csv      0.68M events, 10 MB
  A_qpstat.csv     per-QP retransmit counters at 100 ms
  B_qpstat.csv
  A_perftest.log   perftest's own report, unchanged
  B_perftest.log
```

The trace header carries everything needed to interpret the body:

```
# perftest-hpft qptrace v1
# mode=event
# num_of_qps=4 msg_size=65536 cq_mod=1
# cpu_mhz=2100.000000 t0_tsc=... t0_realtime_ns=...
# margin_s=15 duration_s=60
# sample_start_us=... sample_end_us=...
# qpn 0 1298
...
t_us,qp,msgs
```

Four of those lines matter more than they look:

- **`t0_realtime_ns`** places the trace on the wall clock, which is what
  lets two flows started by different processes be drawn on one time axis.
  `plot_demo.py` gets B's offset from this, not from the `sleep` in the run
  script.
- **`sample_start_us` / `sample_end_us`** are the window perftest's own
  average actually covers, stamped by `catch_alarm` in the same clock as
  the trace. Do not compute it from `margin`/`duration`: `t0` is stamped a
  few hundred ms after the alarm is armed (mostly inside `get_cpu_mhz`),
  and that nominal window measured 415 ms off — worth 1.6% of the reported
  average on a flow that steps rate near an edge.
- **`qpn <index> <lqpn>`** is the join key to `qpstat.csv`.
- **`cq_mod`** is the quantum `msgs` advances in. It is 1 here because
  perftest auto-disables cq_mod above 8192 bytes; at smaller message sizes
  it silently reverts to 100 and you must pass `-Q 1`.

perftest also prints, at the end of the run:

```
qptrace: 2541521 events written to results/A_trace.csv
qptrace: 10666 completions/s/QP -> finest honest bin is ~938 us
```

That second line is the tool reporting its own resolution **at the rate the
run actually achieved** — which nothing can know beforehand.

## 4. How to analyse it

### Reconcile first

```sh
$ python3 ../tools/qptrace_parse.py results/A_trace.csv --bin-us 5000 \
      --sample-window
results/A_trace.csv: mode=event bins=6001 width=5000us span=30.005s qps=4
  ...
  all               21.141
```

perftest's own report for this run says **21.14 Gb/s**. The per-QP series
sums to **21.141**. Always do this before trusting a curve.

### Re-bin freely — that is the point of event mode

The trace holds one record per completion, not a pre-computed rate, so the
bin width is chosen here rather than in the lab. The same run answers
questions at different scales without re-running anything:

```sh
python3 ../tools/qptrace_parse.py results/A_trace.csv --bin-us 500   ...
python3 ../tools/qptrace_parse.py results/A_trace.csv --bin-us 50000 ...
```

Ask for one that is too fine and it refuses to stay quiet:

```
WARNING: a 200 us bin holds 2.23 completions per QP. This is quantisation
noise, not a curve - use --bin-us 895 or coarser.
```

### Plot it

```sh
python3 plot_demo.py            # writes demo.png
```

## 5. Reading the figure

`t = 24.9 s` B enters; `t = 44.5 s` B leaves. Both come from the traces
themselves, not from the script's intended schedule.

| phase | what happens |
|---|---|
| A alone | flat 27.7 Gb/s |
| B enters, t=24.9 | **nothing, for 6.5 s.** Both flows run at ~27.5 — the destination absorbs 55 Gb/s |
| both, t=31.4 on | A and B step down together and settle at ~10.6 each, an even split |
| B leaves, t=44.5 | A starts climbing back |
| A alone, t=53.9 | back to 27.7 — **9.4 s to recover** |

A 6.5 s reaction delay and a 9.4 s recovery are the kind of thing a single
average per run cannot contain and a 1 s report can only hint at. Both
belong to the executor in the path, not to this tool — see section 6.

## 6. Why A dips at t≈8 s, with no B anywhere

Because it has nothing to do with B. A control run — flow A alone for 60 s,
no second flow at any point — reproduces it:

```
A alone, no competitor, Gb/s per second:
27.9 27.7 27.8 27.7 27.8 27.7 27.7 16.8 10.3 10.3 10.3 16.4 26.1 27.5 27.7 ...
                                        ^^^^^^^^^^^^^^^^ ~4 s at 10.3
```

The sender-side rate limiter takes a **one-off excursion in the first ~12 s
of every run**: down to 10.3 Gb/s for about 4 s, then it recovers and stays
up. The timing moves between runs (7 s here, 12 s in an earlier one); the
value does not — always the same 10.3, which is the signature of a fixed
rate step rather than a feedback loop settling.

`doca_pcc` is live on the sender DPU with nothing feeding it targets, and is
the obvious suspect. Not chased further here: this is a demo of the
measurement tool, not of the executor.

It matters for the demo in one way. In an earlier run the excursion happened
to end exactly when B entered, which made it look like B's arrival had
*restored* A's bandwidth. It had not. **B is held back to t=25 so the figure
shows contention rather than that transient** — and the plot still starts at
t=0, so when the excursion does occur it is visible rather than cropped out.

## 7. Smoothness and resolution

The two questions the figure raises. Both have measured answers rather than
aesthetic ones.

### Why 100 ms, and why a *sliding* window

A rate curve is built by asking "how many bytes in this stretch of time?"
With disjoint bins, a smoother line means a wider stretch means **fewer
points**, and the curve degenerates into a staircase.

A sliding window separates the two knobs: the **window** sets how much data
each point averages (smoothness), the **step** sets how many points there
are (density). This figure uses a 100 ms window advanced every 5 ms — 200
points per second regardless of the window.

Measured on a genuinely flat stretch of this run, where the true rate is
constant so all spread is measurement noise:

| window | mean jump between adjacent points | worst |
|---|---|---|
| 10 ms | 2.73% | 14.2% |
| 20 ms | 1.35% | 7.7% |
| 50 ms | 0.55% | 3.2% |
| **100 ms** | **0.30%** | **1.6%** |
| 500 ms | 0.06% | 0.3% |

100 ms is smooth to the eye and still resolves transitions to a tenth of a
second, against events that take seconds. 500 ms would be smoother and would
start rounding off the corners.

### How fine the bins can go

The recording itself has no floor — every completion carries its own
timestamp. The **curve's** floor is set by how often completions happen:

```
finest honest bin ≈ 10 x message size / per-QP rate
```

Measured on one QP over 9 s of constant traffic, 65536 B messages:

| bin | completions/bin | measured rate | noise | empty bins |
|---|---|---|---|---|
| 50 µs | 0.7 | 6.93 | 77.6% | 35% |
| 200 µs | 2.6 | 6.93 | 52.0% | 12% |
| 1 ms | 13.2 | 6.93 | 14.8% | 0% |
| 5 ms | 66 | 6.93 | 4.1% | 0% |
| 50 ms | 661 | 6.93 | 0.4% | 0% |

The measured rate is right at every width; only the noise grows. The tool's
built-in threshold — 10 completions per bin — lands at about 15% noise, which
is a "you can see the shape" line, not a "the number is precise" line.

### Buying resolution with smaller messages

Message size is the lever. Same link, same 4 QPs, `-Q 1` so cq_mod does not
undo it:

| messages | completions/s/QP | finest honest bin | total bandwidth |
|---|---|---|---|
| 65536 B | 10,666 | **938 µs** | 27.7 Gb/s |
| 2048 B + `-Q 1` | 401,997 | **25 µs** | 26.3 Gb/s |

**40x finer, at essentially unchanged bandwidth.** On the 2 KB trace the
noise stops falling below ~50 µs — it flattens at 8-10% from 50 µs out to
1 ms — meaning that is no longer quantisation but the traffic's own
burstiness. So ~25-50 µs is where measuring finer stops adding information.

The cost is volume: 2 KB messages for 12 s produced 18M events and 276 MB;
a 60 s run would be 1.4 GB.

| what you want to see | messages | window |
|---|---|---|
| convergence, steady-state fairness | 65536 B | 20-50 ms |
| a flow entering or leaving | 65536 B | 2-5 ms |
| response to a 1 ms control period | 8192 B + `-Q 1` | 200-500 µs |
| packet-level CC behaviour | 2048 B + `-Q 1` | 25-50 µs |

Below 8192 B, **always pass `-Q 1`** — otherwise `cq_mod` silently reverts
to 100 and resolution gets worse, not better.

## 8. What this demo does not show

- **Per-QP detail.** The figure is per *flow*; the trace is per QP. In this
  run the 4 QPs of each flow coincide to Jain 1.0000 through both rate
  changes — the limiter acts on the flow, not the QP — so drawing them
  separately would draw one line four times.
- **Binned mode.** `--report-interval-us` is exercised in the fork's tests
  but not here.
- **Many QPs.** 4 per flow. At 32+ the per-QP rate drops and the honest bin
  coarsens proportionally.
- **Multiple processes per host.** The lab's real experiments run one
  perftest per VF; traces carry `t0_realtime_ns` so they *can* be merged
  onto one axis — `plot_demo.py` does exactly that for two — but there is no
  general N-way merge in `tools/` yet.
- **Retransmits.** `packet_seq_err` is 0 for both flows here; nothing was
  lost. The counters work — in `../results/twohost_incast_20260728` all four
  of a joining flow's errors landed in one 100 ms sample 0.42 s after it
  joined, while the incumbent recorded none — but on this near-lossless
  fabric they are an event marker, not a rate to plot.
- **Anything about HPFT.** Its agents were off for this run.
