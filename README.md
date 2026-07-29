# perftest-enhanced

A fork of **perftest 6.28** that records **per-QP bandwidth over time**.

Upstream perftest tells you one bandwidth number for a run. If you want to
know what each queue pair was doing at each moment — the thing `iperf3 -P`
gives you for TCP streams — it cannot tell you. This adds that, plus the
offline tools to make the result usable, and changes nothing else.

Baseline commit `5a928eb` is pristine upstream, so **`git diff 5a928eb` is
the entire change**. Every stock test, flag and output is untouched, and an
unpatched perftest works fine as the server for a patched client.

---

## The gap this fills

Stock perftest has two reporting modes, and neither answers "what was each
flow doing, and when":

- the default: **one average for the whole run**;
- `--run_infinitely -D N`: one aggregate line every **N whole seconds**. The
  one-second floor is not a design limit, it is literally `sleep(int)` in
  `handle_signal_print_thread`.

Both are aggregate over all QPs. For comparison, `iperf3 -i` bottoms out at
0.1 s and is per-stream.

The odd part is that perftest **already** attributes every completion to its
QP and never says so: `wr_id` carries the QP index
(`build_wr_id`/`get_wr_id_qp_index`) and `ctx->ccnt[]` is a per-QP array. The
main polling loop does

```c
qp_index = get_wr_id_qp_index(wc[i].wr_id);
ctx->ccnt[qp_index] += fill;
```

and then reports only the sum. This fork records what is already there. It
does not restructure anything.

## What is new

| | |
|---|---|
| `--report-per-qp` and three companions | per-QP bandwidth trace, in the bw tests |
| `tools/qptrace_parse.py` | re-bin, smooth, reconcile, fairness |
| `tools/qptrace_merge.py` | put N traces from N processes on one time axis |
| `tools/qpstat.py` | per-QP retransmit / error counters alongside a run |
| `tools/selfcheck.sh` | 16 assertions on a loopback pair, no root, no second host |
| `demo/` | a complete worked example with a figure |

## Quick start

```sh
# 1. run — the only difference from stock perftest is the two flags
ib_write_bw -d <dev> -s 65536 -q 4 -D 30 --report_gbits \
            --report-per-qp --report-csv=run.csv <peer>

# 2. check the trace agrees with perftest's own number before trusting it
tools/qptrace_parse.py run.csv --bin-us 5000 --sample-window --fairness

# 3. produce a series to plot
tools/qptrace_parse.py run.csv --window-us 100000 --step-us 5000 --out rate.csv
```

## `--report-per-qp`

```
--report-per-qp            enable (bw tests, client side)
--report-interval-us=<us>  bin width; 0 (default) = one record per completion
--report-csv=<file>        output (default perftest_qptrace.csv)
--report-trace-mb=<MB>     preallocated buffer (default 256)
```

**Event mode (the default) is the one you want.** It writes one record per
completion, so the bin width is chosen *offline* — the same run can be
replayed at 100 µs, 1 ms or 10 ms without going back to the wire. Binned mode
(`--report-interval-us > 0`) snapshots the whole `ccnt[]` array on a period;
it exists only for message rates where per-completion records will not fit in
memory, and it costs you the ability to re-bin.

Hot path is one predictable branch plus, when enabled, one rdtsc and one
16-byte store. No signals — perftest's own `handle_signal_print_thread` fires
a SIGALRM per report, which perturbs the polling loop once the period drops
to milliseconds. Measured cost: **+0.009%** of bandwidth at 48K
completions/s, **−0.07%** at 1.45M, both inside run-to-run spread.

The buffer does not wrap. On overflow, recording stops and says so — on
stderr *and* in the CSV header — because a silently truncated curve reads
exactly like a real one.

### The trace file

```
# perftest-enhanced qptrace v1
# mode=event
# num_of_qps=4 msg_size=65536 cq_mod=1 duplex=0
# cpu_mhz=2100.000000 t0_tsc=... t0_realtime_ns=...
# margin_s=15 duration_s=60
# sample_start_us=... sample_end_us=...
# qpn 0 1274
...
t_us,qp,msgs
```

Four header lines carry more than they look:

- **`t0_realtime_ns`** places the trace on the wall clock, which is what lets
  traces from separate processes be merged onto one axis.
- **`sample_start_us` / `sample_end_us`** are the window perftest's own
  average actually covers, stamped by `catch_alarm` in the same clock as the
  trace. Do not derive it from `margin`/`duration`: `t0` is stamped a few
  hundred ms after the alarm is armed (mostly inside `get_cpu_mhz`), measured
  415 ms off — worth 1.6% of the reported average on a flow that changes rate
  near a window edge.
- **`qpn <index> <lqpn>`** is the join key to `qpstat.py` output.
- **`cq_mod`** is the quantum `msgs` advances in. See trap 2.

## Offline tools

### `qptrace_parse.py` — one trace

```sh
tools/qptrace_parse.py run.csv --bin-us 5000 --sample-window --fairness
tools/qptrace_parse.py run.csv --window-us 100000 --step-us 5000 --out r.csv
```

Prints per-QP and total Gb/s over the chosen window, optionally Jain
fairness, and refuses to stay quiet when the requested bin is too fine to
mean anything.

**Prefer `--window-us` when you want a smooth curve.** A wider `--bin-us`
buys smoothness by deleting points, and the line becomes a staircase. A
sliding window keeps the two knobs independent: the *window* sets how much
data each point averages, the *step* sets how many points there are.
Measured on a flat stretch of a real run, adjacent points jump 2.73% at a
10 ms window and 0.30% at 100 ms — at identical point density.

### `qptrace_merge.py` — N traces

```sh
tools/qptrace_merge.py a_trace.csv b_trace.csv --window-us 100000 --out m.csv
tools/qptrace_merge.py run*/trace.csv --bin-us 50000 --per-qp --out m.csv
```

One perftest process can only drive one device, so a many-flow experiment is
usually N processes, each timestamped from its own `t0`. This aligns them via
`t0_realtime_ns`. Exact within a host; across hosts only as good as the clock
sync (NTP measured at tens of milliseconds here, fine for "who got what",
useless for sub-10 ms timing). A flow's rows are absent outside its own
lifetime rather than zero — that is absence, not a rate of zero.

### `qpstat.py` — per-QP retransmits

```sh
sudo tools/qpstat.py --dev <dev>/1 --pid <perftest-pid> \
                     --interval-ms 100 --duration 30 --out qpstat.csv
```

The column iperf3 has next to per-stream bandwidth and RDMA does not: which
flow is retransmitting. Binds a dedicated hardware counter to each of that
process's QPs, samples `packet_seq_err` / `out_of_sequence` /
`local_ack_timeout_err` / …, and unbinds on exit. Joins to the bandwidth
trace on LQPN.

Two facts that force this shape, both verified on mlx5:

- **q_counters carry no byte counts.** `rdma statistic show link <dev>/1`
  yields request and error counters only. Bandwidth *has* to come from the
  application; these counters are the complement, not a substitute.
- **`rdma statistic qp set ... auto type on` does not give each QP its own
  counter** — auto mode binds every QP of a type to one shared set. Per-QP
  numbers need explicit `bind lqpn`, which needs root. Binding mid-run is
  fine; these are cumulative counters, so a late start only costs a prefix.

Sampling floor is ~50 ms (each sample shells out to `rdma`) — three orders
coarser than the bandwidth trace, and the right trade: retransmits are
counted events whose trajectory over seconds is the question.

### `selfcheck.sh` — does it still work

```sh
tools/selfcheck.sh [ibdev] [local-ip]      # loopback, ~90 s, no root
```

16 assertions: reconciliation in both modes, per-QP attribution, the QPN join
key, all four traps below, both resolution warnings, and the offline tools.
Exits non-zero on any failure. Run this first if you have just picked the
fork up.

## Resolution: what you can actually get

The recording has no floor — every completion carries its own timestamp. The
**curve's** floor is set by how often completions happen:

```
finest honest bin ≈ 10 × message size / per-QP rate
```

Measured on one QP over 9 s of genuinely constant traffic, 65536 B messages,
so all spread is measurement noise:

| bin | completions/bin | measured rate | noise | empty bins |
|---|---|---|---|---|
| 50 µs | 0.7 | 6.93 | 77.6% | 35% |
| 200 µs | 2.6 | 6.93 | 52.0% | 12% |
| 1 ms | 13.2 | 6.93 | 14.8% | 0% |
| 5 ms | 66 | 6.93 | 4.1% | 0% |
| 50 ms | 661 | 6.93 | 0.4% | 0% |

The rate is right at every width; only the noise grows. The built-in
threshold of 10 completions per bin lands at about 15% noise — a "you can see
the shape" line, not a "the number is precise" line.

**Message size is the lever.** Same link, same QPs, `-Q 1` so `cq_mod` does
not undo it:

| messages | completions/s/QP | finest honest bin | total bandwidth |
|---|---|---|---|
| 65536 B | 10,624 | **941 µs** | 27.7 Gb/s |
| 2048 B + `-Q 1` | 401,997 | **25 µs** | 26.3 Gb/s |

40× finer at essentially unchanged bandwidth. On the 2 KB trace the noise
stops falling below ~50 µs — flat at 8-10% from 50 µs out to 1 ms — meaning
that is no longer quantisation but the traffic's own burstiness. So ~25-50 µs
is where measuring finer stops adding information.

The cost is volume: 2 KB messages for 12 s produced 18M events and 276 MB.

| what you want to see | messages | window |
|---|---|---|
| convergence, steady-state fairness | 65536 B | 20-50 ms |
| a flow entering or leaving | 65536 B | 2-5 ms |
| response to a ~1 ms control loop | 8192 B + `-Q 1` | 200-500 µs |
| packet-level congestion behaviour | 2048 B + `-Q 1` | 25-50 µs |

## Four things that will silently give you a wrong curve

**1. Not enough completions per bin.** See the table above. At 65536 B with
128 QPs on a 25G link, a 1 ms bin holds 0.37 completions — a pulse train, not
a curve, whatever the tool does. Both the tool (at dump time, against the
rate the run actually achieved — which nothing can know beforehand) and the
parser (at the bin you asked for) compute this and warn.

**2. `cq_mod`.** `ccnt[]` advances in steps of `cq_mod` messages, so that is
the finest resolution a trace can have. perftest auto-disables `cq_mod` only
for `size > 8192`, so **shrinking the message to buy resolution silently
re-enables the default of 100 and makes things worse**: measured, `-s 2048`
gave 1811 completions/s/QP against 6577 at `-s 65536`. Pass `-Q 1` with small
messages. Warned at parse time.

**3. `-b` measures a different quantity than perftest reports.** In duplex
mode perftest adds the remote endpoint's reported bandwidth to its own, while
the trace holds only this endpoint's send completions — so the trace sums to
roughly half the printed figure. Measured 27.60 reported against 14.01
traced, with the trace's event count exactly equal to perftest's own
`#iterations`; the accounting is right, the quantities differ. Warned at
parse time, and `duplex=1` goes in the header so the parser warns too.

**4. This is the sender's completion curve, not the wire.** An RC WRITE
completes when its ACK returns, with `tx_depth` (default 128) messages in
flight, so the curve can lag and smooth relative to what is on the wire.
Checked against receiver-side NIC counters (`results/paired_wire_20260728`):
volume agrees to **0.07%** once MTU-derived header overhead is accounted for,
shape correlates at **r = 0.97** at 5 ms bins, and per-bin variability
matches to three digits — so it is not smoothing away 5 ms structure. What
that run could **not** settle is lag below ~10 ms: cross-host clock alignment
was tens of ms against a `tx_depth` effect of ~2.4 ms. That needs a shared
clock (PTP, or CQE hardware timestamps, which perftest does not use), not
more runs.

## Validation

Measured on the machines this was developed on; `results/` holds the runs and
a `summary.md` each.

| check | result |
|---|---|
| per-QP series vs perftest's own average | 23.138 vs 23.14 Gb/s |
| binned mode vs perftest's own average | 27.576 vs 27.58 Gb/s |
| binned totals vs event-mode totals | 240915 vs 240672 completions |
| rate invariance across bin widths (0.5/1/2/10 ms) | identical to 3 decimals |
| sliding window vs disjoint bins | agree to 0.02% |
| volume vs receiver-side NIC counters | 1.0562 vs 1.0569 predicted from MTU |
| perturbation at 48K events/s | +0.009% |
| perturbation at 1.45M events/s | −0.07% |

## Scope

Only `run_iter_bw` is hooked — for RDMA read/write the client is the only
side that gets completions. `run_iter_bw_server` (send/recv) and the
`run_infinitely` paths are untouched, and `--report-per-qp` is **rejected**
with `-a` and `--run_infinitely` rather than silently producing one trace per
size.

Not implemented, deliberately:

- **CQE hardware timestamps** (`IBV_WC_EX_WITH_COMPLETION_TIMESTAMP`). The
  only way to settle sub-10 ms lag against a receiver, and unnecessary unless
  you need that.
- **send/recv server-side tracing.** Same hook, one function over; add it if
  you need `ib_send_bw` server numbers.

## Demo

`demo/` runs a two-flow contention scenario end to end, traces it, and
renders a figure. `demo/README.md` walks the whole path — the experiment, the
exact command difference from stock perftest, what lands on disk, how to
reconcile and re-bin it, and how to read the result.

---
---

# Upstream perftest README (verbatim)

Everything above is additive. The original documentation follows unchanged.

```
	     Open Fabrics Enterprise Distribution (OFED)
                	Performance Tests README



===============================================================================
Table of Contents
===============================================================================
1. Overview
2. Installation
3. Notes on Testing Methodology
4. Test Descriptions
5. Running Tests
6. Known Issues

===============================================================================
1. Overview
===============================================================================
This is a collection of tests written over uverbs intended for use as a
performance micro-benchmark. The tests may be used for HW or SW tuning
as well as for functional testing.

The collection contains a set of bandwidth and latency benchmark such as:

	* Send        - ib_send_bw and ib_send_lat
	* RDMA Read   - ib_read_bw and ib_read_lat
	* RDMA Write  - ib_write_bw and ib_write_lat
	* RDMA Atomic - ib_atomic_bw and ib_atomic_lat
	* Native Ethernet (when working with MOFED2) - raw_ethernet_bw, raw_ethernet_lat

Please post results/observations to the openib-general mailing list.
See "Contact Us" at http://openib.org/mailman/listinfo/openib-general and
http://www.openib.org.

===============================================================================
2. Installation
===============================================================================
-After cloning the repository a perftest directory should appear in your current
 directory

-Cloning example :
git clone <URL>, In our situation its --> git clone https://github.com/linux-rdma/perftest.git

-After cloning, Follow this commands:

-cd perftest/

-./autogen.sh

-./configure    Note:If you want to install in a specific directory use the optional flag --prefix=<Directory path> , e.g: ./configure --prefix=<Directory path>

-make

-make install

-All of the tests will appear in the  perftest directory and in the install directory.
===============================================================================
3. Notes on Testing Methodology
===============================================================================
- The benchmarks use the CPU cycle counter to get time stamps without context
  switch.  Some CPU architectures (e.g., Intel's 80486 or older PPC) do not
  have such capability.

- The latency benchmarks measure round-trip time but report half of that as one-way
  latency. This means that the results may not be accurate for asymmetrical configurations.

- On all unidirectional bandwidth benchmarks, the client measures the bandwidth.
  On bidirectional bandwidth benchmarks, each side measures the bandwidth of
  the traffic it initiates, and at the end of the measurement period, the server
  reports the result to the client, who combines them together.

- Latency tests report minimum, median and maximum latency results. 
  The median latency is typically less sensitive to high latency variations,
  compared to average latency measurement.
  Typically, the first value measured is the maximum value, due to warmup effects.

- Long sampling periods have very limited impact on measurement accuracy.
  The default value of 1000 iterations is pretty good.
  Note that the program keeps data structures with memory footprint proportional
  to the number of iterations. Setting a very high number of iteration may
  have negative impact on the measured performance which are not related to
  the devices under test.
  If a high number of iterations is strictly necessary, it is recommended to
  use the -N flag (No Peak).

- Bandwidth benchmarks may be run for a number of iterations, or for a fixed duration.
  Use the -D flag to instruct the test to run for the specified number of seconds.
  The --run_infinitely flag instructs the program to run until interrupted by
  the user, and print the measured bandwidth every 5 seconds. 

- The "-H" option in latency benchmarks dumps a histogram of the results.
  See xgraph, ygraph, r-base (http://www.r-project.org/), PSPP, or other 
  statistical analysis programs.

  *** IMPORTANT NOTE:
      When running the benchmarks over an Infiniband fabric,
      a Subnet Manager must run on the switch or on one of the
      nodes in your fabric, prior to starting the benchmarks.

Architectures tested:	i686, x86_64, ia64

===============================================================================
4. Benchmarks Description
===============================================================================

The benchmarks generate a synthetic stream of operations, which is very useful
for hardware and software benchmarking and analysis.
The benchmarks are not designed to emulate any real application traffic.
Real application traffic may be affected by many parameters, and hence
might not be predictable based only on the results of those benchmarks.

ib_send_lat 	latency test with send transactions
ib_send_bw 	bandwidth test with send transactions
ib_write_lat 	latency test with RDMA write transactions
ib_write_bw 	bandwidth test with RDMA write transactions
ib_read_lat 	latency test with RDMA read transactions
ib_read_bw 	bandwidth test with RDMA read transactions
ib_atomic_lat	latency test with atomic transactions
ib_atomic_bw 	bandwidth test with atomic transactions

Raw Ethernet interface benchmarks:
raw_ethernet_send_lat  latency test over raw Ethernet interface
raw_ethernet_send_bw   bandwidth test over raw Ethernet interface

===============================================================================
5. Running Tests
===============================================================================

Prerequisites:
	kernel 2.6
	(kernel module) matches libibverbs
	(kernel module) matches librdmacm
	(kernel module) matches libibumad
	(kernel module) matches libmath (lm)
	(linux kernel module) matches pciutils (lpci).

Server:		./<test name> <options>
Client:		./<test name> <options> <server IP address>

		o  <server address> is IPv4 or IPv6 address. You can use the IPoIB
                   address if IPoIB is configured.
		o  --help lists the available <options>

  *** IMPORTANT NOTE:
      The SAME OPTIONS must be passed to both server and client.

Common Options to all tests:
----------------------------
  -h, --help				Display this help message screen
  -p, --port=<port>			Listen on/connect to port <port> (default: 18515)
  -R, --rdma_cm				Connect QPs with rdma_cm and run test on those QPs
  -z, --comm_rdma_cm			Communicate with rdma_cm module to exchange data - use regular QPs
  -m, --mtu=<mtu>			QP Mtu size (default: active_mtu from ibv_devinfo)
  -c, --connection=<type>		Connection type RC/UC/UD/XRC/DC/SRD (default RC).
  -d, --ib-dev=<dev>			Use IB device <dev> (default: first device found)
  -i, --ib-port=<port>			Use network port <port> of IB device (default: 1)
  -s, --size=<size>			Size of message to exchange (default: 1)
  -a, --all				Run sizes from 2 till 2^23
  -n, --iters=<iters>			Number of exchanges (at least 100, default: 1000)
  -x, --gid-index=<index>		Test uses GID with GID index taken from command
  -V, --version				Display version number
  -e, --events				Sleep on CQ events (default poll)
  -F, --CPU-freq			Do not fail even if cpufreq_ondemand module
  -I, --inline_size=<size>		Max size of message to be sent in inline mode
  -u, --qp-timeout=<timeout>		QP timeout = (4 uSec)*(2^timeout) (default: 14)
  -S, --sl=<sl>				Service Level (default 0)
  -r, --rx-depth=<dep>			Receive queue depth (default 600)

Options for latency tests:
--------------------------

  -C, --report-cycles			Report times in CPU cycle units
  -H, --report-histogram		Print out all results (Default: summary only)
  -U, --report-unsorted			Print out unsorted results (default sorted)

Options for BW tests:
---------------------

  -b, --bidirectional			Measure bidirectional bandwidth (default uni)
  -N, --no peak-bw			Cancel peak-bw calculation (default with peak-bw)
  -Q, --cq-mod				Generate Cqe only after <cq-mod> completion
  -t, --tx-depth=<dep>			Size of tx queue (default: 128)
  -O, --dualport			Run test in dual-port mode (2 QPs). Both ports must be active (default OFF)
  -D, --duration=<sec> 			Run test for <sec> period of seconds
  -f, --margin=<sec> 			When in Duration, measure results within margins (default: 2)
  -l, --post_list=<list size>		Post list of send WQEs of <list size> size (instead of single post)
      --recv_post_list=<list size>	Post list of receive WQEs of <list size> size (instead of single post)
  -q, --qp=<num of qp's>		Num of QPs running in the process (default: 1)
      --run_infinitely			Run test until interrupted by user, print results every 5 seconds

SEND tests (ib_send_lat or ib_send_bw) flags: 
---------------------------------------------

  -r, --rx-depth=<dep>			Size of receive queue (default: 512 in BW test)
  -g, --mcg=<num_of_qps> 		Send messages to multicast group with <num_of_qps> qps attached to it
  -M, --MGID=<multicast_gid>		In multicast, uses <multicast_gid> as the group MGID

WRITE latency (ib_write_lat) flags:
-----------------------------------

  --write_with_imm				Use write-with-immediate verb instead of write

ATOMIC tests (ib_atomic_lat or ib_atomic_bw) flags: 
---------------------------------------------------

  -A, --atomic_type=<type>		type of atomic operation from {CMP_AND_SWAP,FETCH_AND_ADD}
  -o, --outs=<num>			Number of outstanding read/atomic requests - also on READ tests

Options for raw_ethernet_send_bw:
---------------------------------
  -B, --source_mac			source MAC address by this format XX:XX:XX:XX:XX:XX (default take the MAC address form GID)
  -E, --dest_mac			destination MAC address by this format XX:XX:XX:XX:XX:XX **MUST** be entered
  -J, --server_ip			server ip address by this format X.X.X.X (using to send packets with IP header)
  -j, --client_ip			client ip address by this format X.X.X.X (using to send packets with IP header)
  -K, --server_port			server udp port number (using to send packets with UDP header)
  -k, --client_port			client udp port number (using to send packets with UDP header)
  -Z, --server				choose server side for the current machine (--server/--client must be selected)
  -P, --client				choose client side for the current machine (--server/--client must be selected)

----------------------------------------------
Special feature detailed explanation in tests:
----------------------------------------------

  1. Usage of post_list feature (-l, --post_list=<list size> and --recv_post_list=<list size>)
     In this case, each QP will prepare <list size> WQEs (instead of 1), and will chain them to each other.
     In chaining we mean allocating <list_size> array, and setting 'next' pointer of each WQE in the array
     to point to the following element in the array. the last WQE in the array will point to NULL.
     In this case, when posting the first WQE in the list, will instruct the HW to post all of those WQEs.
     Which means each post send/recv will post <list_size> messages.
     This feature is good if we want to know the maximum message rate of QPs in a single process.
     Since we are limited to SW posts (for example, on post_send ~ 10 Mpps, since we have ~ 500 ns between
     each SW post_send), we can see the true HW message rate when setting <list_size> of 64 (for example)
     since it's not depended on SW limitations.

  2. RDMA Connected Mode (CM)
     You can add the "-R" flag to all tests to connect the QPs from each side with the rdma_cm library.
     In this case, the library will connect the QPs and will use the IPoIB interface for doing it.
     It helps when you don't have Ethernet connection between the 2 nodes.
     You must supply the IPoIB interface as the server IP.

  3. Multicast support in ib_send_lat and in ib_send_bw
     Send tests have built in feature of testing multicast performance, in verbs level.
     You can use "-g" to specify the number of QPs to attach to this multicast group.
     "-M" flag allows you to choose the multicast group address.

  4. GPUDirect usage:
     As of perftest release 25.07 the build system automatically
     detects the location of cuda.h. Passing CUDA_H_PATH to the configure
     script is therefore no longer required. The variable is still accepted
     for backward-compatibility but its usage is not recommended.
     The variable will depracted in the 25.10 release.

     For perftest releases earlier than 25.07 you must still provide the path to
     cuda.h explicitly during configuration, for example:
     ./autogen.sh && ./configure CUDA_H_PATH=/usr/local/cuda/include/cuda.h && make -j

     Thus --use_cuda=<gpu_index> flag will be available to add to a command line:
     ./ib_write_bw -d ib_dev --use_cuda=<gpu index> -a

    CUDA DMA-BUF requierments:
        1) CUDA Toolkit 11.7 or later.
        2) NVIDIA Open-Source GPU Kernel Modules version 515 or later.
           installation instructions: http://us.download.nvidia.com/XFree86/Linux-x86_64/515.43.04/README/kernel_open.html
        3) Configuration / Usage:
          export the following environment variables:
            1- export LD_LIBRARY_PATH.
              e.g: export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
            2- export LIBRARY_PATH.
              e.g: export LIBRARY_PATH=/usr/local/cuda/lib64:$LIBRARY_PATH
          perform compilation as decribe in the begining of section 4 (GPUDirect usage).

    CUDA Runtime API support:
      To use the --gpu_touch option in Perftest, you must build Perftest with support for the CUDA Runtime API (libcudart).
      Run the configure script with the following flags:
      ./configure --enable-cudart

      For releases earlier than 25.07:
      ./configure CUDA_H_PATH=/usr/local/cuda/include/cuda.h --enable-cudart

      Note: Ensure that your NVIDIA CUDA Compiler (nvcc) version is compatible with your GCC version. Incompatibility between nvcc and gcc can cause build or runtime issues.

  5. AES_XTS (encryption/decryption)
     In perftest repository there are two files as follow:
      1) gen_data_enc_key.c

      2) encrypt_credentials.c

      gen_data_enc_key.c file should be compiled with the following command:
      #gcc gen_data_enc_key.c -o gen_data_enc_key -lcrypto

      encrypt_credentials.c file should be compiled with the following command:
      #gcc encrypt_credentials.c -o encrypt_credentials -lcrypto

      You must provide the plaintext credentials and the kek in seperate files in hex format.

      for example:

      credential_file:
        0x00
        0x00
        0x00
        0x00
        0x10
        etc..

      kek_file:
        0x00
        0x00
        0x11
        0x22
        0x55
        etc..

      Notes:
        1) You should run the encrypt_credentials program and give paths as parameters
        to the plaintext credential_file, kek_file and the path you want the encrypted
        credentials to be in (credentials_file first).
        for example:
          #./encrypt_credentials <PATH>/credential_file <PATH>/kek_file
          <PATH>/encrypted_credentials_file_name

        The output of this is a text file that you must provide its path
        as a parameter to the perftest application with --credentials_path <PATH>

        2)Both encrypt_credentials.c and gen_data_enc_key.c should be compiled
          before using the perftest application.

        3)gen_data_enc_key.c compiled program path must be provided to the perftest
          application with --data_enc_key_app_path <PATH> and the kek file should be
          provided with --kek_path <PATH>

        4) This feature supported only on RC qp type, and on ib_write_bw, ib_read_bw,
           ib_send_bw, ib_read_lat, ib_send_lat.

        5) You should load the kek and credentials you want to the device in the following way:
          #sudo mlxreg -d <pci address> --reg_name CRYPTO_OPERATIONAL --set "credential[0]
          =0x00000000,credential[1]=0x10000000,credential[2]=0x10000000,
          credential[3]=0x10000000,credential[4]=0x10000000,credential[5]=0x10000000
          ,credential[6]=0x10000000,credential[7]=0x10000000,credential[8]=0x10000000
          ,credential[9]=0x10000000,kek[0]=0x00001122,kek[1]=0x55556633,kek[2]=0x33447777,kek[3]=0x22337777"

  6. Payload modification
        Using the --payload_file_path you can pass a text file, which contains a pattern,
        as a parameter to perftest, and use the pattern as the payload of the RDMA verb.

        You must provide the pattern in DWORD's seperated by comma and in hex format.

        for example:
        0xddccbbaa,0xff56f00d,0xffffffff,0x21ab025b, etc...

        Notes:
          1) Perftest parse the pattern and save it in LE format.
          2) The feature available for ib_write_bw, ib_read_bw, ib_send_bw, ib_read_lat and ib_send_lat.
          3) 0 size pattern is not allow.


===============================================================================
7. Known Issues
===============================================================================

 1. Multicast support in ib_send_lat and in ib_send_bw is not stable.
    The benchmark program may hang or exhibit other unexpected behavior.

 2. Bidirectional support in ib_send_bw test, when running in UD or UC mode.
    In rare cases, the benchmark program may hang.
    perftest-2.3 release includes a feature for hang detection, which will exit test after 2 mins in those situations.

 3. Different versions of perftest may not be compatible with each other.
    Please use the same perftest version on both sides to ensure consistency of benchmark results.

 4. Test version 5.3 and above won't work with previous versions of perftest. As well as 5.70 and above.

 5. This perftest package won't compile on MLNX_OFED-2.1 due to API changes in MLNX_OFED-2.2
    In order to compile it properly, please do:
    ./configure --disable-verbs_exp
    make

 6. In the x390x platform virtualized environment the results shown by package test applications can be incorrect.

 7. perftest-2.3 release includes support for dualport VPI test - port1-Ethernet , port2-IB. (in addition to Eth:Eth, IB:IB)
    Currently, running dualport when port1-IB , port2-Ethernet still not working.

 8. If GPUDirect is not working, (e.g. you see "Couldn't allocate MR" error message), consider disabling Scatter to CQE feature. Set the environmental variable MLX5_SCATTER_TO_CQE=0. E.g.:
    MLX5_SCATTER_TO_CQE=0 ./ib_write_bw -d ib_dev --use_cuda=<gpu index> -a

 9. When using high number of qps (>2K) with message size larger than 8KB, BW may degrade. perftest will set the polling batch to 64.
  In higher scales, consider using --cqe_poll to set the number of CQE's that polled every iteration to be higher than default value.

 10. Number of QPs limitation: Perftest uses a single Completion Queue (CQ) per direction (send and receive) for all Queue Pairs (QPs). The CQ capacity is limited by the max_cqe allowed by device. The total number of Completion Queue Entries (CQEs) required depends on:
  The transmit (tx-depth) and receive (rx-depth) depths.

 11. Iterations Mode and SRQ Depth: When running in iterations mode, the receive depth (rx-depth) is limited by the number of iterations per QP, which defaults to 1000. In certain scenarios, this may lead to a shortage of Work Queue Entries (WQEs) for the associated QPs. To mitigate this:
  Increase the Shared Receive Queue (SRQ) depth as needed (--rx-depth).

 12. Outstanding Reads in ib_read_bw: When using ib_read_bw with a high number of QPs, the default value for outstanding reads is set to the maximum allowed by the device. This can cause backpressure in packet processing, leading to reduced performance. To address this:
  Consider reducing the number of outstanding reads to alleviate backpressure and improve throughput (--outs).
  Tune this parameter based on your specific workload and hardware capabilities.
```
