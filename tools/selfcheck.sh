#!/usr/bin/env bash
# Copyright (c) 2026 perftest-enhanced contributors.  All rights reserved.
# SPDX-License-Identifier: GPL-2.0-only OR BSD-2-Clause
#
# Dual licensed under GPL v2 or the OpenIB.org BSD license; see COPYING in
# the main directory of this source tree.
# Verify the fork still does what it claims, on a loopback pair.
#
# This exists because the fork is being archived: the next person to touch
# it needs one command that says whether it still works, without a lab
# booking or knowledge of what to look for. Every check asserts something
# the README or the module docs promise.
#
# Loopback only - server and client on one device, talking to the local IP.
# It puts real traffic on the wire briefly but needs no second host, no
# root, and no configuration.
#
# usage: selfcheck.sh [ibdev] [local-ip] [port-base]
set -u
DEV=${1:-mlx5_6}; IP=${2:-10.1.0.1}; PORT=${3:-18800}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BW=$ROOT/ib_write_bw
PARSE=$ROOT/tools/qptrace_parse.py
MERGE=$ROOT/tools/qptrace_merge.py
TMP=$(mktemp -d); trap 'rm -rf "$TMP"; pkill -x ib_write_bw 2>/dev/null' EXIT
PASS=0; FAIL=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
check(){ if [ "$1" = 1 ]; then ok "$2"; else bad "$2${3:+ -- $3}"; fi; }

# Run one loopback pair. Extra args go to BOTH ends except the --report-*
# ones, which are client-side only: $1 = shared args, $2 = client-only args.
run() {
	local shared="$1" cliopt="$2"
	PORT=$((PORT+1))
	pkill -x ib_write_bw 2>/dev/null; sleep 0.4
	# shellcheck disable=SC2086
	( $BW -d "$DEV" -p $PORT $shared >"$TMP/srv.log" 2>&1 & ) ; sleep 1.2
	# shellcheck disable=SC2086
	$BW -d "$DEV" -p $PORT $shared $cliopt "$IP" \
		>"$TMP/cli.log" 2>"$TMP/cli.err"
}

# perftest's own reported Gb/s, and the trace's total over the same window.
reported() { awk '$1+0==$1 && NF>=4 {print $4; exit}' "$TMP/cli.log"; }
traced()   { python3 "$PARSE" "$1" --bin-us 5000 --sample-window 2>/dev/null \
             | awk '$1=="all"{print $NF}'; }
close()    { python3 -c "
import sys
a,b,tol=float(sys.argv[1] or 0),float(sys.argv[2] or 0),float(sys.argv[3])
print(1 if a>0 and b>0 and abs(a-b)/a<tol else 0)" "$1" "$2" "$3"; }

echo "perftest-enhanced selfcheck: dev=$DEV ip=$IP"
echo

echo "reconciliation - the trace must sum to what perftest reports"
for q in 1 8; do
	run "-s 65536 -q $q -D 8 --report_gbits" \
	    "--report-per-qp --report-csv=$TMP/q$q.csv"
	r=$(reported); t=$(traced "$TMP/q$q.csv")
	check "$(close "$r" "$t" 0.01)" "-q $q event mode: perftest ${r:-?} vs trace ${t:-?} Gb/s" \
	      "differ by more than 1%"
done

run "-s 65536 -q 4 -D 8 --report_gbits" \
    "--report-per-qp --report-interval-us=1000 --report-csv=$TMP/bin.csv"
r=$(reported); t=$(traced "$TMP/bin.csv")
check "$(close "$r" "$t" 0.02)" "binned mode: perftest ${r:-?} vs trace ${t:-?} Gb/s" \
      "differ by more than 2%"

echo
echo "per-QP accounting"
n=$(python3 -c "
import collections,sys
c=collections.Counter()
for ln in open('$TMP/q8.csv'):
    if ln[0] not in '#t': c[ln.split(',')[1]]+=1
print(len(c))")
check "$([ "$n" = 8 ] && echo 1 || echo 0)" "-q 8 produced 8 distinct QP series" "got $n"
check "$(grep -qc '^# qpn 7 ' "$TMP/q8.csv" >/dev/null && echo 1 || echo 0)" \
      "QPN map present for every QP (join key for qpstat.py)"

echo
echo "the traps the tool is supposed to shout about"
run "-s 2048 -q 4 -D 6 --report_gbits" \
    "--report-per-qp --report-csv=$TMP/small.csv"
check "$(grep -q 'cq_mod is only auto-disabled' "$TMP/cli.log" && echo 1 || echo 0)" \
      "small messages without -Q 1: cq_mod trap is warned about"
check "$(grep -q 'cq_mod=100' "$TMP/small.csv" && echo 1 || echo 0)" \
      "  and cq_mod=100 is recorded in the header"

run "-s 2048 -q 4 -D 6 -Q 1 --report_gbits" \
    "--report-per-qp --report-csv=$TMP/small1.csv"
check "$(grep -q 'cq_mod=1 ' "$TMP/small1.csv" && echo 1 || echo 0)" \
      "-Q 1 restores per-message resolution"

run "-s 65536 -q 4 -D 8 --report_gbits" \
    "--report-per-qp --report-trace-mb=1 --report-csv=$TMP/ovf.csv"
check "$(grep -q 'TRUNCATED' "$TMP/ovf.csv" && echo 1 || echo 0)" \
      "buffer overflow is recorded in the trace header"
check "$(grep -q 'buffer full' "$TMP/cli.err" && echo 1 || echo 0)" \
      "  and shouted on stderr, not swallowed"

run "-s 65536 -q 4 -D 8 -b --report_gbits" \
    "--report-per-qp --report-csv=$TMP/bi.csv"
check "$(grep -q 'measures a DIFFERENT quantity' "$TMP/cli.log" && echo 1 || echo 0)" \
      "-b: the duplex quantity mismatch is warned about"
check "$(grep -q 'duplex=1' "$TMP/bi.csv" && echo 1 || echo 0)" \
      "  and duplex=1 is recorded so the parser can warn too"

echo
echo "resolution honesty"
check "$(grep -q 'finest honest bin' "$TMP/cli.log" && echo 1 || echo 0)" \
      "dump reports the achieved-rate resolution floor"
check "$(python3 "$PARSE" "$TMP/q8.csv" --bin-us 50 2>&1 >/dev/null \
        | grep -qc 'quantisation noise' && echo 1 || echo 0)" \
      "parser refuses to draw a 50 us bin quietly"

echo
echo "offline analysis"
python3 "$PARSE" "$TMP/q8.csv" --bin-us 5000 --sample-window \
	--window-us 100000 --out "$TMP/slide.csv" >/dev/null 2>&1
s=$(python3 -c "
import csv,collections
d=collections.defaultdict(list)
for r in csv.DictReader(x for x in open('$TMP/slide.csv') if not x.startswith('#')):
    d[r['qp']].append(float(r['gbps']))
print('%.3f'%sum(sum(v)/len(v) for v in d.values()))")
b=$(traced "$TMP/q8.csv")
check "$(close "$b" "$s" 0.02)" "sliding window agrees with disjoint bins (${b:-?} vs ${s:-?})"

python3 "$MERGE" "$TMP/q1.csv" "$TMP/q8.csv" --window-us 100000 \
	--out "$TMP/merged.csv" >"$TMP/merge.log" 2>&1
check "$(grep -qc '^t_s,source' "$TMP/merged.csv" >/dev/null && echo 1 || echo 0)" \
      "merge puts two traces on one axis"

echo
echo "parallel QP setup"
run "-s 65536 -q 16 -D 6 --report_gbits --setup-threads=1" ""
ser=$(reported)
run "-s 65536 -q 16 -D 6 --report_gbits --setup-threads=8" ""
par=$(reported)
check "$(close "$ser" "$par" 0.05)" \
      "threaded setup matches serial (${ser:-?} vs ${par:-?} Gb/s)"

echo
echo "punctual start"
T=$(python3 -c "import time;print(int(time.time())+8)")
run "-s 65536 -q 4 -D 6 --report_gbits --start_at=$T" \
    "--report-per-qp --report-csv=$TMP/start.csv"
off=$(python3 -c "
import re
t0=[int(re.search(r't0_realtime_ns=(\d+)',l).group(1))
    for l in open('$TMP/start.csv') if 't0_realtime_ns' in l]
print('%.1f' % ((t0[0]/1e9-$T)*1e3) if t0 else 'nan')")
check "$(python3 -c "
try: print(1 if abs(float('$off')) < 20 else 0)
except ValueError: print(0)")" \
      "--start_at releases the first packet on time ($off ms late)" \
      "every slow step must run before the deadline, not after it"

echo
echo "rate limit through the threaded connect"
run "-s 8192 -q 4 -D 6 --report_gbits" "--rate_limit=5"
r=$(reported)
check "$(close "$r" 5 0.05)" "--rate_limit=5 delivers ${r:-?} Gb/s" \
      "on RoCE the hardware limiter is refused, so the fall-back to the software one must survive the parallel setup path"
check "$(grep -q 'providing SW rate limit' "$TMP/cli.err" && echo 1 || echo 0)" \
      "  and the fall-back is announced, not silent"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ] || exit 1
