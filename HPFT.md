# perftest-hpft

Fork of perftest 6.28 that adds what iperf3 gives you and RDMA does not:
**per-QP bandwidth over time, plus per-QP retransmits.**

Upstream perftest already attributes every completion to its QP — `wr_id`
carries the index (`build_wr_id`/`get_wr_id_qp_index`) and `ctx->ccnt[]` is a
per-QP array — but only ever reports the sum. The patch records what is
already there; it does not restructure anything.

Baseline commit `5a928eb` is pristine upstream, so `git diff 5a928eb` is the
whole change.

## Bandwidth: `--report-per-qp`

```
--report-per-qp            enable (BW tests, client side)
--report-interval-us=<us>  bin width; 0 (default) = one record per CQE
--report-csv=<file>        output (default perftest_qptrace.csv)
--report-trace-mb=<MB>     preallocated buffer (default 256)
```

```sh
ib_write_bw -d mlx5_6 -s 65536 -q 8 -D 30 --report_gbits \
            --report-per-qp --report-csv=run.csv 10.1.0.2
tools/qptrace_parse.py run.csv --bin-us 5000 --sample-window --fairness
```

**Event mode (the default) is the one you want.** It writes one record per
completion, so the bin width is chosen offline — the same run can be replayed
at 100 us, 1 ms or 10 ms without going back to the lab. Binned mode
(`--report-interval-us > 0`) snapshots the whole `ccnt[]` array on a period
and is only for message rates where per-CQE records will not fit in memory;
it costs you the ability to re-bin.

Hot path is one predictable branch plus, when enabled, one rdtsc and one
16-byte store. No signals — perftest's existing `handle_signal_print_thread`
fires a SIGALRM per report, which perturbs the polling loop once the period
drops to milliseconds. The buffer does not wrap: on overflow recording stops
and says so, on stderr and in the CSV header, because a silently truncated
curve reads exactly like a real one.

`# qpn <index> <lqpn>` lines in the CSV header are the join key to the
retransmit counters below.

## Offline: `tools/qptrace_parse.py`, `tools/qptrace_merge.py`

`qptrace_parse.py` re-bins one trace and reconciles it against perftest's
own average. Two ways to shape the series:

```sh
tools/qptrace_parse.py run.csv --bin-us 5000 --sample-window --fairness
tools/qptrace_parse.py run.csv --window-us 100000 --step-us 5000 --out r.csv
```

Prefer `--window-us` when you want a smooth curve. A wider `--bin-us` buys
smoothness by deleting points and the line becomes a staircase; a sliding
window keeps the two independent - the window sets how much data each point
averages, the step sets how many points there are. Measured on a flat
stretch of a real run, adjacent points jump 2.73% at a 10 ms window and
0.30% at 100 ms, at identical point density.

`qptrace_merge.py` puts several traces on one axis, which is what a run of
"128 flows" actually is - one perftest process per VF, each timestamped from
its own t0. Alignment uses `t0_realtime_ns`, so it is exact within a host
and only as good as NTP across hosts (tens of ms, measured).

```sh
tools/qptrace_merge.py vf*/A_trace.csv --window-us 100000 --out merged.csv
```

## Retransmits: `tools/qpstat.py`

```sh
tools/qpstat.py --dev mlx5_6/1 --pid $(pgrep -x ib_write_bw) \
                --interval-ms 100 --duration 30 --out qpstat.csv
```

Binds a dedicated hardware counter to each of the run's QPs, samples
`packet_seq_err` / `out_of_sequence` / `local_ack_timeout_err` / … and
unbinds on exit. Needs root. Joins to the bandwidth trace on LQPN.

Sampling floor is ~50 ms (each sample shells out to `rdma`), which is three
orders coarser than the bandwidth trace and is the right trade: retransmits
are counted events whose trajectory over seconds is the question.

## Checking it still works

```sh
tools/selfcheck.sh [ibdev] [local-ip]      # loopback, ~90 s, no root
```

16 assertions covering reconciliation in both modes, per-QP attribution, the
QPN join key, every trap below, the resolution warnings, and the two offline
tools. Exits non-zero on any failure.

## Three things that will silently give you a wrong curve

**1. Not enough completions per bin.** A per-QP series is a curve only if
each bin holds enough completions:

```
completions per QP per bin = per-QP rate x bin / message size
```

At `-s 65536` with 128 QPs on a 25G link that is **0.37 per 1 ms bin** — the
result is a pulse train, not a curve, whatever the tool does. Both the tool
(at dump time, against the rate the run actually achieved) and the parser (at
the bin you asked for) compute this and refuse to stay quiet about it.

**2. cq_mod.** `ccnt[]` advances in steps of `cq_mod` messages, so that is the
finest resolution the trace can have. perftest only auto-disables cq_mod for
`size > 8192`, so **shrinking the message to buy resolution silently
re-enables the default of 100 and makes things worse**: measured on loopback,
`-s 2048` gave 1811 completions/s/QP against 6577 at `-s 65536`. Pass `-Q 1`
with small messages.

**3. `-b` measures a different quantity than perftest reports.** In duplex
mode perftest adds the remote endpoint's reported bandwidth to its own
([`perftest_parameters.c`](src/perftest_parameters.c) `print_full_bw_report`),
while the trace holds only this endpoint's send completions - so the trace
sums to roughly half the printed figure, and the two must not be reconciled.
Warned at parse time, and `duplex=1` goes in the trace header so the parser
warns too.

**4. This is the sender's completion curve, not the wire.** An RC WRITE
completes when the ACK returns, and `tx_depth` (default 128) messages are in
flight, so the curve can lag and smooth relative to what is actually on the
wire. Measured against receiver-side hardware counters
(`results/paired_wire_20260728`): volume agrees to **0.07%** once MTU-derived
header overhead is accounted for, shape correlates at **r = 0.97** at 5 ms
bins, and per-bin variability is the same to three digits — so it is not
smoothing away 5 ms structure. What that run could **not** settle is lag
below ~10 ms, because cross-host NTP alignment is tens of ms against a
`tx_depth` effect of ~2.4 ms; that needs a shared clock, not more runs. The
wire also shows excursions above the sender's rate, most plausibly
retransmitted arrivals that never complete — unproven. For what arrived, use
receiver-side hardware counters.

## Validation (loopback, sgpu01 mlx5_6, 8 QPs)

| check | result |
|---|---|
| per-QP series vs perftest's own average, sample window | 27.584 vs 27.58 Gb/s |
| binned mode vs perftest's own average | 27.576 vs 27.58 Gb/s |
| binned totals vs event-mode totals | 240915 vs 240672 completions |
| rate invariance across bin widths (0.5/1/2/10 ms) | identical to 3 decimals |
| perturbation at 48K events/s | +0.009% |
| perturbation at 1.45M events/s | −0.07% |

Both perturbation figures are inside run-to-run spread (±0.02 Gb/s).

## Scope

Only `run_iter_bw` is hooked — for read/write the client is the only side
that gets CQEs. `run_iter_bw_server` (send/recv) and the `run_infinitely`
paths are untouched; `--report-per-qp` is rejected with `-a` and
`--run_infinitely` rather than silently producing one trace per size.
