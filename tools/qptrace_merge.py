#!/usr/bin/env python3
"""Put several traces on one time axis.

The lab's real experiments run one perftest process per VF, so a run that is
conceptually "128 flows" arrives as N separate traces, each timestamped from
its own t0. This aligns them and emits one series.

Alignment uses the `t0_realtime_ns` in each header, so it is exact for
traces taken on the SAME host - one CLOCK_REALTIME, no assumptions. Across
hosts it is only as good as their clock sync: NTP was measured at tens of
milliseconds between two machines here, which is fine for seeing which flow
got what and useless for comparing sub-10 ms timing. Nothing in this script
can detect that case, so mind it yourself.

Smoothing follows qptrace_parse: --window-us gives a sliding window (each
point averages that much data, points --step-us apart, so smoothness and
point density stay independent), --bin-us gives disjoint bins.

usage:
  qptrace_merge.py A_trace.csv B_trace.csv --window-us 100000 --out m.csv
  qptrace_merge.py vf*/trace.csv --bin-us 50000 --per-qp --out m.csv
"""
import argparse
import bisect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qptrace_parse import read_trace                       # noqa: E402


def label_for(path):
    base = os.path.basename(path)
    for suffix in ("_trace.csv", ".csv"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    if base in ("trace", ""):                  # vf0/trace.csv -> vf0
        base = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return base


def load(path):
    meta, qpn, _cols, rows = read_trace(path)
    if not rows:
        sys.exit("%s holds no records" % path)
    if meta.get("mode") != "event":
        sys.exit("%s is a binned capture; merging needs event mode "
                 "(--report-interval-us=0)" % path)
    if "t0_realtime_ns" not in meta:
        sys.exit("%s has no t0_realtime_ns and cannot be aligned" % path)
    nqps, size = int(meta["num_of_qps"]), int(meta["msg_size"])
    if "truncated" in meta:
        print("WARNING: %s is truncated; its tail is missing." % path,
              file=sys.stderr)
    if meta.get("duplex", "0") != "0":
        print("WARNING: %s was taken with -b; it holds only that endpoint's "
              "send completions." % path, file=sys.stderr)

    # Cumulative message count per QP, on the shared wall clock in seconds.
    t0 = int(meta["t0_realtime_ns"]) / 1e9
    per_qp = [[] for _ in range(nqps)]
    for r in rows:
        per_qp[int(r[1])].append((t0 + float(r[0]) / 1e6, int(r[2])))
    cum = []
    for q in range(nqps):
        per_qp[q].sort()
        ts = [t for t, _ in per_qp[q]]
        acc, run = [], 0
        for _, n in per_qp[q]:
            run += n
            acc.append(run)
        cum.append((ts, acc))
    return {"label": label_for(path), "nqps": nqps, "size": size, "cum": cum,
            "qpn": qpn, "lo": min(c[0][0] for c in cum if c[0]),
            "hi": max(c[0][-1] for c in cum if c[0])}


def msgs_between(cum, q, a, b):
    ts, acc = cum[q]
    i, j = bisect.bisect_left(ts, a), bisect.bisect_left(ts, b)
    return (acc[j - 1] if j else 0) - (acc[i - 1] if i else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("traces", nargs="+")
    ap.add_argument("--window-us", type=float,
                    help="sliding window width (preferred; see module doc)")
    ap.add_argument("--step-us", type=float,
                    help="sliding-window step (default: window / 20)")
    ap.add_argument("--bin-us", type=float,
                    help="disjoint bin width, if you do not want a window")
    ap.add_argument("--per-qp", action="store_true",
                    help="one row per QP instead of one per trace")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if not args.window_us and not args.bin_us:
        args.window_us = 100000.0
    if args.window_us and args.bin_us:
        sys.exit("give --window-us or --bin-us, not both")

    srcs = [load(p) for p in args.traces]
    if len({s["label"] for s in srcs}) != len(srcs):
        sys.exit("trace labels are not unique: %s"
                 % sorted(s["label"] for s in srcs))

    origin = min(s["lo"] for s in srcs)
    end = max(s["hi"] for s in srcs)
    width = (args.window_us or args.bin_us) / 1e6
    step = (args.step_us / 1e6 if args.step_us else
            (width / 20.0 if args.window_us else width))
    half = width / 2.0 if args.window_us else 0.0

    print("merging %d traces over %.3f s" % (len(srcs), end - origin))
    for s in srcs:
        tot = sum(msgs_between(s["cum"], q, s["lo"], s["hi"])
                  for q in range(s["nqps"]))
        span = s["hi"] - s["lo"]
        print("  %-12s %2d QPs  active %7.3f - %7.3f s  mean %6.2f Gb/s"
              % (s["label"], s["nqps"], s["lo"] - origin, s["hi"] - origin,
                 tot * s["size"] * 8 / span / 1e9 if span > 0 else 0))

    with open(args.out, "w") as f:
        f.write("# qptrace_merge: %s\n"
                % ("sliding window=%.0fus step=%.0fus"
                   % (args.window_us, step * 1e6) if args.window_us
                   else "bins=%.0fus" % args.bin_us))
        f.write("# origin_realtime_ns=%d\n" % int(origin * 1e9))
        f.write("t_s,source,qp,lqpn,gbps\n")
        x, npts = origin + half, 0
        while x <= end - half:
            for s in srcs:
                # Outside a trace's own lifetime the flow was not there. That
                # is absence, not a zero rate, so emit nothing rather than
                # draw a line along the floor.
                if x < s["lo"] or x > s["hi"]:
                    continue
                a, b = (x - half, x + half) if half else (x, x + width)
                if args.per_qp:
                    for q in range(s["nqps"]):
                        g = (msgs_between(s["cum"], q, a, b) * s["size"] * 8
                             / width / 1e9)
                        f.write("%.6f,%s,%d,%s,%.6f\n"
                                % (x - origin, s["label"], q,
                                   s["qpn"].get(q, ""), g))
                else:
                    m = sum(msgs_between(s["cum"], q, a, b)
                            for q in range(s["nqps"]))
                    f.write("%.6f,%s,all,,%.6f\n"
                            % (x - origin, s["label"],
                               m * s["size"] * 8 / width / 1e9))
            x += step
            npts += 1
    print("  wrote %s (%d time points)" % (args.out, npts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
