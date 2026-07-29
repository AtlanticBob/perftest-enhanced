#!/usr/bin/env python3
"""Turn a --report-per-qp trace into per-QP rate series at a chosen bin width.

The point of event mode is that the bin width is NOT baked into the capture:
one run can be re-binned here at 100 us, 1 ms or 10 ms without going back to
the lab. Binned captures can only be re-binned to multiples of what they were
recorded at, and this refuses to pretend otherwise.

The honesty check is the same one the tool prints at dump time, restated
against the bin you actually asked for: a per-QP series is a curve only if
each bin holds enough completions. Below that it is quantisation noise, and
smoothing it does not add information - it just hides the problem.

usage:
  qptrace_parse.py trace.csv --bin-us 1000 --out rates.csv
  qptrace_parse.py trace.csv --bin-us 5000 --sample-window --fairness
"""
import argparse
import sys

MIN_EVENTS_PER_BIN = 10


def read_trace(path):
    meta, qpn, rows, cols = {}, {}, [], None
    with open(path) as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if ln.startswith("#"):
                body = ln[1:].strip()
                if body.startswith("qpn "):
                    _, idx, num = body.split()
                    qpn[int(idx)] = int(num)
                elif body.startswith("TRUNCATED"):
                    meta["truncated"] = body
                else:
                    for tok in body.split():
                        if "=" in tok:
                            k, v = tok.split("=", 1)
                            meta[k] = v
                continue
            if cols is None:
                cols = ln.split(",")
                continue
            rows.append(ln.split(","))
    return meta, qpn, cols, rows


def series_from_events(rows, nqps, bin_us, size):
    """(bin index -> per-QP bytes) from one record per completion."""
    bins = {}
    for r in rows:
        b = int(float(r[0]) // bin_us)
        q, n = int(r[1]), int(r[2])
        bins.setdefault(b, [0] * nqps)[q] += n * size
    return bins


def series_from_bins(rows, nqps, bin_us, size, rec_us):
    """(bin index -> per-QP bytes) by differencing cumulative snapshots."""
    if bin_us < rec_us:
        sys.exit("trace was recorded at %.0f us bins; cannot re-bin finer. "
                 "Re-run in event mode (--report-interval-us=0) if you need "
                 "to choose the width offline." % rec_us)
    bins, prev, prev_b = {}, None, None
    for r in rows:
        t = float(r[0])
        cur = [int(x) for x in r[1:1 + nqps]]
        b = int(t // bin_us)
        if prev is not None and b != prev_b:
            bins[prev_b] = [(c - p) * size for c, p in zip(cur, prev)]
            prev = cur
        elif prev is None:
            prev, prev_b = cur, b
            continue
        prev_b = b
    return bins


def jain(xs):
    xs = [x for x in xs]
    if not xs or not any(xs):
        return float("nan")
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--bin-us", type=float, default=1000.0)
    ap.add_argument("--out", help="write per-QP rates here (t_s,qp,gbps)")
    ap.add_argument("--sample-window", action="store_true",
                    help="keep only perftest's own [margin, duration-margin]"
                         " window, i.e. what its reported average covers")
    ap.add_argument("--fairness", action="store_true",
                    help="also print Jain fairness across QPs per bin")
    args = ap.parse_args()

    meta, qpn, cols, rows = read_trace(args.trace)
    if not rows:
        sys.exit("%s holds no records" % args.trace)
    if "truncated" in meta:
        print("WARNING: trace is truncated (%s); the tail is missing."
              % meta["truncated"], file=sys.stderr)

    nqps = int(meta["num_of_qps"])
    size = int(meta["msg_size"])
    mode = meta["mode"]

    if mode == "event":
        bins = series_from_events(rows, nqps, args.bin_us, size)
    else:
        rec_us = (float(rows[1][0]) - float(rows[0][0])) if len(rows) > 1 \
            else args.bin_us
        bins = series_from_bins(rows, nqps, args.bin_us, size, rec_us)

    if args.sample_window:
        # Prefer the window catch_alarm() actually stamped over the nominal
        # [margin, duration - margin]: the SIGALRM lands late by an unknown
        # amount, and on a flow that steps rate near an edge that shifts the
        # average by percent, not by rounding.
        if "sample_start_us" in meta:
            lo = float(meta["sample_start_us"])
            hi = float(meta["sample_end_us"])
        else:
            lo = float(meta["margin_s"]) * 1e6
            hi = float(meta["duration_s"]) * 1e6 - lo
        klo, khi = int(lo // args.bin_us), int(hi // args.bin_us)
    else:
        klo, khi = min(bins), max(bins) + 1

    # Every bin in the range, not just the ones that saw a completion: a bin
    # with no completions is a real zero. Dropping them shortens the span and
    # inflates every rate - by 30% at 2 ms on a bursty run, in testing.
    keys = list(range(klo, khi))
    for k in keys:
        bins.setdefault(k, [0] * nqps)
    if not keys:
        sys.exit("no bins in the selected window")

    # Restate the resolution check against the bin actually requested.
    tot_msgs = sum(sum(bins[k]) for k in keys) / size
    per_qp_per_bin = tot_msgs / nqps / len(keys)
    if per_qp_per_bin < MIN_EVENTS_PER_BIN:
        print("WARNING: a %.0f us bin holds %.2f completions per QP. This is "
              "quantisation noise, not a curve - use --bin-us %.0f or coarser."
              % (args.bin_us, per_qp_per_bin,
                 args.bin_us * MIN_EVENTS_PER_BIN / max(per_qp_per_bin, 1e-9)),
              file=sys.stderr)

    sec = args.bin_us / 1e6
    totals = [0] * nqps
    for k in keys:
        for q in range(nqps):
            totals[q] += bins[k][q]
    span = len(keys) * sec

    print("%s: mode=%s bins=%d width=%.0fus span=%.3fs qps=%d"
          % (args.trace, mode, len(keys), args.bin_us, span, nqps))
    print("  %-4s %-8s %10s" % ("qp", "lqpn", "Gb/s"))
    for q in range(nqps):
        print("  %-4d %-8s %10.3f"
              % (q, qpn.get(q, "-"), totals[q] * 8 / span / 1e9))
    print("  %-4s %-8s %10.3f" % ("all", "", sum(totals) * 8 / span / 1e9))
    if args.fairness:
        js = [jain(bins[k]) for k in keys]
        js = [j for j in js if j == j]
        if js:
            js.sort()
            print("  jain across QPs: mean %.4f  p05 %.4f  min %.4f"
                  % (sum(js) / len(js), js[len(js) // 20], js[0]))

    if args.out:
        with open(args.out, "w") as f:
            f.write("t_s,qp,lqpn,gbps\n")
            t0 = keys[0]
            for k in keys:
                for q in range(nqps):
                    f.write("%.6f,%d,%s,%.6f\n"
                            % ((k - t0) * sec, q, qpn.get(q, ""),
                               bins[k][q] * 8 / sec / 1e9))
        print("  wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
