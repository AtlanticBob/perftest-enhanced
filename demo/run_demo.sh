#!/usr/bin/env bash
# Demo experiment: one number vs the curve.
#
# A single RDMA flow is joined partway through by a second one aimed at the
# same destination VF, then left alone again. Stock perftest reports ONE
# average for each flow. The timings below are chosen so that A's reported
# average is a rate A was never actually at:
#
#   t=0    A: sgpu01 vf0 -> sgpu02 vf0, 4 QPs      -D 60
#   t=25   B: sgpu01 vf3 -> sgpu02 vf0, 4 QPs      -D 20
#   t=45   B ends
#   t=60   A ends
#
# The five phases the figure has to show: A alone, B enters, both, B leaves,
# A alone again.
#
# Why B does not enter until t=25: the sender-side rate limiter takes a
# one-off excursion somewhere in the first ~12 s of every run - it drops to
# 10.3 Gb/s for about 4 s and then recovers for good. It is nothing to do
# with contention; a control run of A completely alone reproduces it (see
# README section 4). B enters well after it, so the figure shows contention
# rather than that transient, and the plot starts at t=15 for the same
# reason.
#
# The two source VFs are picked so they do not share a hardware rate-limiter
# budget - on this fabric two of the four VFs hash together, and such a pair
# would look throttled for a reason unrelated to contention. Check your own
# fabric before assuming two sources are independent.
#
# Machine names and addresses below are the ones this was run on. ~65 s.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
OUT="$DIR/results"; mkdir -p "$OUT"
PT=$DIR/../ib_write_bw                              # patched
PT_SRV=$HOME/hyperfront/perftest-26015/ib_write_bw  # stock, server side
TOOLS=$DIR/../tools

DST=10.1.0.2; SRV_DEV=mlx5_6
PA=18970; PB=18971
QN=4; SZ=65536
DUR_A=60; DUR_B=20; JOIN=25

ssh -o BatchMode=yes sgpu02 "pkill -x ib_write_bw 2>/dev/null; true"
pkill -x ib_write_bw 2>/dev/null; sleep 1

# -D is a NEGOTIATED parameter: each server's duration must equal its
# client's exactly, or the handshake fails with "duration mismatch".
ssh -o BatchMode=yes -f sgpu02 "setsid nohup $PT_SRV -d $SRV_DEV -p $PA \
  -s $SZ -q $QN --report_gbits -D $DUR_A >/tmp/demo_s$PA.log 2>&1 </dev/null &"
ssh -o BatchMode=yes -f sgpu02 "setsid nohup $PT_SRV -d $SRV_DEV -p $PB \
  -s $SZ -q $QN --report_gbits -D $DUR_B >/tmp/demo_s$PB.log 2>&1 </dev/null &"
sleep 2

echo "t=0   A starts (vf0 -> $DST)"
# The only difference from a normal invocation is the three --report-* flags.
$PT -d mlx5_6 -p $PA -s $SZ -q $QN --report_gbits -D $DUR_A \
    --report-per-qp --report-csv="$OUT/A_trace.csv" $DST \
    >"$OUT/A_perftest.log" 2>&1 &
APID=$!
sleep 2
sudo -n "$TOOLS/qpstat.py" --dev mlx5_6/1 --pid $APID --interval-ms 100 \
    --duration $((DUR_A-3)) --out "$OUT/A_qpstat.csv" >"$OUT/A_qpstat.log" 2>&1 &

sleep $((JOIN-2))
echo "t=$JOIN  B joins (vf3 -> $DST)"
$PT -d mlx5_9 -p $PB -s $SZ -q $QN --report_gbits -D $DUR_B \
    --report-per-qp --report-csv="$OUT/B_trace.csv" $DST \
    >"$OUT/B_perftest.log" 2>&1 &
BPID=$!
sleep 1
sudo -n "$TOOLS/qpstat.py" --dev mlx5_9/1 --pid $BPID --interval-ms 100 \
    --duration $((DUR_B-2)) --out "$OUT/B_qpstat.csv" >"$OUT/B_qpstat.log" 2>&1 &

wait $APID $BPID 2>/dev/null
sleep 2
echo "done -> $OUT"
grep -hE "^ $SZ" "$OUT"/A_perftest.log "$OUT"/B_perftest.log
