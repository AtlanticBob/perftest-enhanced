#!/usr/bin/env python3
"""Per-QP retransmit/error counters alongside a perftest run.

This is the column iperf3 gives you next to per-stream bandwidth and RDMA
does not: which flow is retransmitting. It is deliberately a separate
process rather than part of perftest.

Why it has to work this way:

  - mlx5 q_counters carry NO byte counts (verified: `rdma statistic show
    link mlx5_6/1` yields rx_write_requests, packet_seq_err,
    local_ack_timeout_err ... and nothing in bytes). Bandwidth therefore
    has to come from the application - that is what --report-per-qp does.
    These counters are the complement, not a substitute.

  - `rdma statistic qp set link DEV auto type on` does NOT give each QP its
    own counter: auto mode binds every QP of the same type to one shared
    counter set. Per-QP numbers require an explicit `bind lqpn`, which is
    what this does. Verified on live RTS QPs mid-transfer: each bind
    allocates a fresh cntn whose lqpn list holds exactly that one QP.

  - binding needs root, and binding mid-run is fine: these are cumulative
    counters and what matters is their trajectory, so a late start only
    costs a prefix.

Resolution: each sample shells out to `rdma`, which costs milliseconds, so
the floor here is ~50 ms - three orders coarser than the bandwidth trace.
That is the right trade: retransmits are counted events whose trajectory
over seconds is the question, not a waveform.

Join key: the LQPN in this CSV matches the `# qpn <index> <lqpn>` lines in
the perftest --report-csv header, which is what ties a retransmit burst to
the QP whose bandwidth collapsed.

usage:
  qpstat.py --dev mlx5_6/1 --comm ib_write_bw --duration 30 --out qpstat.csv
  qpstat.py --dev mlx5_6/1 --pid 12345 --interval-ms 200 --out qpstat.csv
"""
import argparse
import json
import signal
import subprocess
import sys
import time

# Every numeric field the driver exposes is recorded; these are the ones
# that actually move under congestion, and lead the CSV for readability.
LEAD = [
    "packet_seq_err",           # PSN gap: the GBN retransmit trigger
    "out_of_sequence",
    "duplicate_request",
    "implied_nak_seq_err",
    "local_ack_timeout_err",
    "rnr_nak_retry_err",
    "req_transport_retries_exceeded",
    "req_rnr_retries_exceeded",
    "rx_write_requests",
    "rx_read_requests",
]


def rdma(*args, root=False):
    cmd = (["sudo", "-n"] if root else []) + ["rdma", "-j"] + list(args)
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd),
                                              out.stderr.strip()))
    return json.loads(out.stdout or "[]")


def find_qps(dev, pid, comm):
    """RC QPs on dev belonging to the traffic process."""
    qps = []
    for q in rdma("resource", "show", "qp", "link", dev):
        if q.get("type") != "RC":
            continue
        if pid is not None and q.get("pid") != pid:
            continue
        if comm is not None and q.get("comm") != comm:
            continue
        qps.append(q)
    return qps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", required=True, help="ibdev/port, e.g. mlx5_6/1")
    ap.add_argument("--pid", type=int, help="only QPs of this pid")
    ap.add_argument("--comm", help="only QPs of this comm, e.g. ib_write_bw")
    ap.add_argument("--interval-ms", type=float, default=100.0)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--wait", type=float, default=30.0,
                    help="seconds to wait for the QPs to appear")
    ap.add_argument("--out", default="qpstat.csv")
    args = ap.parse_args()

    if args.pid is None and args.comm is None:
        # Binding every RC QP on a shared device would steal counters from
        # whatever else is running; make the caller say what they mean.
        ap.error("give --pid or --comm so we bind only the run's QPs")

    if args.interval_ms < 50:
        print("qpstat: interval floored at 50 ms (each sample is an "
              "`rdma` invocation)", file=sys.stderr)
        args.interval_ms = 50.0

    deadline = time.time() + args.wait
    qps = []
    while time.time() < deadline:
        qps = find_qps(args.dev, args.pid, args.comm)
        if qps:
            break
        time.sleep(0.05)
    if not qps:
        print("qpstat: no matching RC QPs on %s after %.0fs" %
              (args.dev, args.wait), file=sys.stderr)
        return 1
    lqpns = sorted(q["lqpn"] for q in qps)
    print("qpstat: %d QPs on %s: %s" % (len(lqpns), args.dev, lqpns))

    bound = []
    try:
        for q in lqpns:
            try:
                rdma("statistic", "qp", "bind", "link", args.dev,
                     "lqpn", str(q), root=True)
            except RuntimeError as e:
                print("qpstat: bind lqpn %d failed: %s" % (q, e),
                      file=sys.stderr)
        # Read back which counters we actually own, so teardown only
        # removes ours and a partial bind is visible rather than silent.
        for c in rdma("statistic", "qp", "show", "link", args.dev, root=True):
            if len(c.get("lqpn", [])) == 1 and c["lqpn"][0] in lqpns:
                bound.append(c["cntn"])
        if len(bound) < len(lqpns):
            print("qpstat: WARNING bound %d of %d QPs; the rest have no "
                  "per-QP counter and are absent from the output"
                  % (len(bound), len(lqpns)), file=sys.stderr)
        if not bound:
            return 1

        stop = [False]
        signal.signal(signal.SIGINT, lambda *a: stop.__setitem__(0, True))
        signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__(0, True))

        fields, rows = None, []
        t0 = time.time()
        nxt = t0
        while not stop[0] and time.time() - t0 < args.duration:
            now = time.time()
            if now < nxt:
                time.sleep(min(nxt - now, 0.01))
                continue
            nxt += args.interval_ms / 1000.0
            if nxt < now:            # overran: do not try to catch up
                nxt = now + args.interval_ms / 1000.0
            snap = rdma("statistic", "qp", "show", "link", args.dev,
                        root=True)
            ts = time.time()
            for c in snap:
                if c.get("cntn") not in bound or len(c.get("lqpn", [])) != 1:
                    continue
                if fields is None:
                    rest = sorted(k for k, v in c.items()
                                  if isinstance(v, int) and k not in LEAD
                                  and k not in ("cntn", "ifindex", "port"))
                    fields = [f for f in LEAD if f in c] + rest
                rows.append((ts - t0, c["lqpn"][0],
                             [c.get(f, 0) for f in fields]))

        with open(args.out, "w") as f:
            f.write("# perftest-enhanced qpstat v1\n")
            f.write("# dev=%s interval_ms=%.0f\n" % (args.dev,
                                                     args.interval_ms))
            f.write("# t0_realtime_ns=%d\n" % int(t0 * 1e9))
            f.write("# lqpn joins the '# qpn <index> <lqpn>' lines in the "
                    "perftest --report-csv header\n")
            f.write("t_s,lqpn," + ",".join(fields or []) + "\n")
            for t, q, vals in rows:
                f.write("%.3f,%d,%s\n" % (t, q,
                                          ",".join(str(v) for v in vals)))
        print("qpstat: %d samples written to %s" % (len(rows), args.out))
    finally:
        for c in bound:
            try:
                rdma("statistic", "qp", "unbind", "link", args.dev,
                     "cntn", str(c), root=True)
            except RuntimeError as e:
                print("qpstat: unbind cntn %d failed: %s" % (c, e),
                      file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
