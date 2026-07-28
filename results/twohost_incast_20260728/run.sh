#!/usr/bin/env bash
# Two-host validation of --report-per-qp and tools/qpstat.py under real
# congestion. Loopback proved the accounting reconciles and costs nothing;
# it could not prove the two things that only exist on a real fabric:
#   (a) per-QP curves that actually separate when flows contend
#   (b) per-QP retransmit counters that actually move
#
# Incast, one destination VF, with a join and a leave so the trace has to
# capture dynamics rather than a flat line:
#
#   t=0    A: sgpu01 vf0 -> sgpu02 vf0, 4 QPs      alone
#   t=15   B: sgpu01 vf3 -> sgpu02 vf0, 4 QPs      joins, 2:1 incast
#   t=45   B leaves                                A alone again
#   t=60   A ends
#
# vf0 and vf3 as the two sources deliberately: vf1 and vf2 hash to the same
# PCC flowtag for every dst, so that pair would share one budget and muddy
# the result for an unrelated reason.
#
# Reads nothing from and writes nothing to the hpft repos - no registry
# edits, no agent restarts. HPFT's rx/tx agents are inactive during this
# run, so what is being exercised is the native RoCE path.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
OUT="$DIR/results"; mkdir -p "$OUT"
PT=$DIR/../../ib_write_bw                       # patched, client side
PT_SRV=$HOME/hyperfront/perftest-26015/ib_write_bw   # stock, server side
TOOLS=$DIR/../../tools

DST=10.1.0.2; SRV_DEV=mlx5_6                    # sgpu02 vf0
PA=18960; PB=18961
QN=4; SZ=65536
DUR_A=60; DUR_B=30; JOIN=15

ssh -o BatchMode=yes sgpu02 "pkill -x ib_write_bw 2>/dev/null; true"
pkill -x ib_write_bw 2>/dev/null; sleep 1

# Servers wait on accept, so both can start now; their duration timers only
# begin once their client connects. -D is a negotiated parameter, so each
# server's duration must equal its client's exactly or the handshake fails.
ssh -o BatchMode=yes -f sgpu02 \
  "setsid nohup $PT_SRV -d $SRV_DEV -p $PA -s $SZ -q $QN --report_gbits \
     -D $DUR_A >/tmp/incast_srv_$PA.log 2>&1 </dev/null &"
ssh -o BatchMode=yes -f sgpu02 \
  "setsid nohup $PT_SRV -d $SRV_DEV -p $PB -s $SZ -q $QN --report_gbits \
     -D $DUR_B >/tmp/incast_srv_$PB.log 2>&1 </dev/null &"
sleep 2

echo "t=0  A starts (vf0 -> $DST)"
$PT -d mlx5_6 -p $PA -s $SZ -q $QN --report_gbits -D $DUR_A \
    --report-per-qp --report-csv="$OUT/A_trace.csv" $DST \
    >"$OUT/A_perftest.log" 2>&1 &
APID=$!
sleep 2
sudo -n "$TOOLS/qpstat.py" --dev mlx5_6/1 --pid $APID --interval-ms 100 \
    --duration $((DUR_A-3)) --out "$OUT/A_qpstat.csv" \
    >"$OUT/A_qpstat.log" 2>&1 &

sleep $((JOIN-2))
echo "t=$JOIN  B joins (vf3 -> $DST)"
$PT -d mlx5_9 -p $PB -s $SZ -q $QN --report_gbits -D $DUR_B \
    --report-per-qp --report-csv="$OUT/B_trace.csv" $DST \
    >"$OUT/B_perftest.log" 2>&1 &
BPID=$!
sleep 1
sudo -n "$TOOLS/qpstat.py" --dev mlx5_9/1 --pid $BPID --interval-ms 100 \
    --duration $((DUR_B-2)) --out "$OUT/B_qpstat.csv" \
    >"$OUT/B_qpstat.log" 2>&1 &

wait $APID $BPID 2>/dev/null
sleep 2
echo "done"
