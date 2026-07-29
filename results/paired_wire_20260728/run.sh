#!/usr/bin/env bash
# Same incast as results/twohost_incast_20260728, with the receiver's
# hardware wire counters collected alongside the sender's completion trace.
#
# The open question from that run: an RC WRITE completes when its ACK
# returns and tx_depth (128) messages are in flight, so the sender's
# completion curve could lag or smooth what is actually on the wire. Its
# two-step rate-down might be the executor, or might be that window. One
# curve cannot tell the difference; two can.
#
#   sender   sgpu01, per-QP completion trace     (--report-per-qp)
#   wire     hpft-dpu2, QUERY_VPORT_COUNTER      (1 ms, vport 1 = dst vf0)
#
# The vport meter already publishes at 1 ms; only the lab's sampler reads it
# at 1 Hz. vpm_sample_fast.py polls faster than the writer and dedupes on
# the published timestamp, so this is the meter's native cadence.
#
# Both clocks are recorded so the two hosts' series can be aligned:
# CLOCK_REALTIME on each side, plus the DPU's CLOCK_MONOTONIC.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
OUT="$DIR/results"; mkdir -p "$OUT"
PT=$DIR/../../ib_write_bw
PT_SRV=$HOME/hyperfront/perftest-26015/ib_write_bw
PREV=$DIR/../twohost_incast_20260728

DST=10.1.0.2; SRV_DEV=mlx5_6
PA=18960; PB=18961
QN=4; SZ=65536
DUR_A=60; DUR_B=30; JOIN=13

ssh -o BatchMode=yes sgpu02 "pkill -x ib_write_bw 2>/dev/null; true"
pkill -x ib_write_bw 2>/dev/null; sleep 1
scp -q "$PREV/vpm_sample_fast.py" hpft-dpu2:/tmp/

# -D is a negotiated parameter: each server's duration must equal its
# client's exactly or the handshake fails.
ssh -o BatchMode=yes -f sgpu02 \
  "setsid nohup $PT_SRV -d $SRV_DEV -p $PA -s $SZ -q $QN --report_gbits \
     -D $DUR_A >/tmp/pw_srv_$PA.log 2>&1 </dev/null &"
ssh -o BatchMode=yes -f sgpu02 \
  "setsid nohup $PT_SRV -d $SRV_DEV -p $PB -s $SZ -q $QN --report_gbits \
     -D $DUR_B >/tmp/pw_srv_$PB.log 2>&1 </dev/null &"

ssh -o BatchMode=yes -f hpft-dpu2 \
  "setsid nohup sudo python3 /tmp/vpm_sample_fast.py 72 /tmp/vpm_fast.csv \
     >/tmp/vpm_fast.log 2>&1 </dev/null &"
sleep 3
date +%s%N > "$OUT/sender_epoch_at_start.txt"

echo "t=0  A starts"
$PT -d mlx5_6 -p $PA -s $SZ -q $QN --report_gbits -D $DUR_A \
    --report-per-qp --report-csv="$OUT/A_trace.csv" $DST \
    >"$OUT/A_perftest.log" 2>&1 &
APID=$!
sleep $JOIN
echo "t=$JOIN  B joins"
$PT -d mlx5_9 -p $PB -s $SZ -q $QN --report_gbits -D $DUR_B \
    --report-per-qp --report-csv="$OUT/B_trace.csv" $DST \
    >"$OUT/B_perftest.log" 2>&1 &
BPID=$!

wait $APID $BPID 2>/dev/null
sleep 10                                  # let the 72 s sampler finish
scp -q hpft-dpu2:/tmp/vpm_fast.csv "$OUT/wire.csv"
ssh -o BatchMode=yes hpft-dpu2 'cat /tmp/vpm_fast.log' | tail -2
echo "done"
