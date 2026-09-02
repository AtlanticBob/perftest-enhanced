/*
 * Copyright (c) 2026 perftest-enhanced contributors.  All rights reserved.
 *
 * This software is available to you under a choice of one of two
 * licenses.  You may choose to be licensed under the terms of the GNU
 * General Public License (GPL) Version 2, available from the file
 * COPYING in the main directory of this source tree, or the
 * OpenIB.org BSD license below:
 *
 *     Redistribution and use in source and binary forms, with or
 *     without modification, are permitted provided that the following
 *     conditions are met:
 *
 *      - Redistributions of source code must retain the above
 *        copyright notice, this list of conditions and the following
 *        disclaimer.
 *
 *      - Redistributions in binary form must reproduce the above
 *        copyright notice, this list of conditions and the following
 *        disclaimer in the documentation and/or other materials
 *        provided with the distribution.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
 * BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
 * ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 * CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
/* perftest_qptrace: per-QP bandwidth-over-time tracing.
 *
 * Why this exists: upstream perftest already attributes every completion to
 * its QP - the wr_id carries the qp index (build_wr_id/get_wr_id_qp_index)
 * and ctx->ccnt[] is a per-QP array - but it only ever reports the sum.
 * Everything needed for an iperf3-style per-stream curve is already in the
 * hot loop; this module just records it.
 *
 * Two record modes, chosen by --report-interval-us:
 *
 *   event mode (interval 0, the default): one record per CQE holding
 *     (tsc, qp, msgs). The bin width is NOT decided at capture time - the
 *     offline parser picks it, so one run can be re-binned at 100us / 1ms /
 *     10ms without re-running the lab. This is the mode you want.
 *
 *   binned mode (interval > 0): snapshot the whole ccnt[] array every
 *     interval. Only for message rates where per-CQE records would not fit
 *     in memory (small messages at line rate); costs you the ability to
 *     re-bin offline.
 *
 * Hot-path cost is one predictable branch plus, when enabled, one rdtsc and
 * one 16-byte store. No syscalls, no locks (the bw polling loop is single
 * threaded), no signals - deliberately NOT reusing perftest's
 * handle_signal_print_thread, whose SIGALRM cadence would perturb the
 * polling loop once the period drops to milliseconds.
 *
 * The buffer is preallocated and does NOT wrap: on overflow recording stops
 * and dump() reports loudly how many records were lost and at what time.
 * A silently truncated curve reads as a real one; a loud stop does not.
 *
 * Resolution caveat, enforced in force_dependencies(): a per-QP curve is
 * only as good as the number of completions per bin, which is
 * (per_qp_rate * bin) / msg_size. At 64KB messages and 128 QPs on a 25G
 * link that is 0.37 completions per 1ms bin - the result is a pulse train,
 * not a curve, whatever the tool does. Note also that cq_mod quantises
 * ccnt[] in steps of cq_mod messages, and that perftest only auto-disables
 * cq_mod for size > MSG_SIZE_CQ_MOD_LIMIT (8192) - so shrinking the message
 * to buy resolution silently re-enables the default cq_mod of 100 unless
 * -Q 1 is passed.
 */
#ifndef PERFTEST_QPTRACE_H
#define PERFTEST_QPTRACE_H

#include <stdint.h>
#include "get_clock.h"

enum qptrace_mode {
	QPTRACE_OFF = 0,
	QPTRACE_EVENT,
	QPTRACE_BIN,
};

struct qptrace_evt {		/* 16 B */
	uint64_t tsc;
	uint32_t qp;
	uint32_t msgs;		/* == cq_mod, except for the last partial
				 * batch in ITERATIONS mode (fill_count) */
};

struct qptrace_state {
	int			mode;		/* hot-path gate, checked first */
	int			nqps;
	uint64_t		n;		/* records used */
	uint64_t		cap;		/* records allocated */
	uint64_t		lost;		/* records dropped after overflow */
	uint64_t		overflow_tsc;	/* when the buffer filled */

	struct qptrace_evt	*evt;		/* event mode */

	uint64_t		*bin_tsc;	/* binned mode: cap entries */
	uint64_t		*bin_ccnt;	/* binned mode: cap * nqps */
	uint64_t		period_cyc;
	uint64_t		next_tsc;

	/* clock and run metadata, all resolved at init */
	uint64_t		t0_tsc;
	uint64_t		t0_real_ns;
	double			mhz;
	uint64_t		msg_size;
	int			cq_mod;
	int			margin;
	int			duration;
	uint32_t		*qpn;		/* nqps entries: index -> QPN */
	const char		*csv_path;
	/* Kept so dump can read the sample window catch_alarm() stamped into
	 * tposted[0]/tcompleted[0]. Those are in the same get_cycles() clock
	 * as the trace, so the window can be reported exactly instead of
	 * derived from margin/duration - which is off by however late the
	 * SIGALRM landed, and that error is amplified whenever the rate
	 * changes near a window edge. */
	struct perftest_parameters *up;
	int			test_type;
	int			duplex;
};

extern struct qptrace_state qptrace;

/* Setup is split in two because everything slow has to happen before the
 * run's clock starts. prepare() measures the CPU clock (get_cpu_mhz busy-
 * waits 219 ms), allocates the trace buffer and faults in every page of it;
 * it is called before the duration alarm is armed and before --start_at
 * releases the first packet. start() only stamps t0, and is called
 * immediately after the alarm, so t0 is the alarm's origin to within
 * microseconds.
 *
 * dump() still reports the sample window from the timestamps catch_alarm()
 * left in tposted[0]/tcompleted[0] rather than from margin/duration: those
 * are in this same clock and carry however late the SIGALRM actually
 * landed, which margin/duration cannot.
 *
 * prepare() returns 0 on success and leaves mode == QPTRACE_OFF when
 * tracing was not requested; start() is then a no-op. */
struct pingpong_context;
struct perftest_parameters;
int qptrace_prepare(struct pingpong_context *ctx,
		    struct perftest_parameters *user_param, int num_of_qps);
void qptrace_start(void);

/* Write the CSV and free. Safe to call when tracing is off. */
void qptrace_dump(void);

/* Hot path: one completion, attributed to qp_index, worth msgs messages. */
static inline void qptrace_event(int qp_index, int msgs)
{
	struct qptrace_state *q = &qptrace;
	struct qptrace_evt *e;

	if (q->mode != QPTRACE_EVENT)
		return;
	if (q->n >= q->cap) {
		if (!q->lost++)
			q->overflow_tsc = get_cycles();
		return;
	}
	e = &q->evt[q->n++];
	e->tsc = get_cycles();
	e->qp = (uint32_t)qp_index;
	e->msgs = (uint32_t)msgs;
}

/* Binned mode: called at the top of the outer posting loop. Snapshots the
 * whole per-QP counter array when the period has elapsed. The recorded
 * timestamp is the actual snapshot time, not the nominal deadline, because
 * the loop can be late; and an overrun does not try to catch up: a period
 * that was missed is gone, not owed. */
static inline void qptrace_tick(const uint64_t *ccnt)
{
	struct qptrace_state *q = &qptrace;
	uint64_t now, *row;
	int i;

	if (q->mode != QPTRACE_BIN)
		return;
	now = get_cycles();
	if (now < q->next_tsc)
		return;
	if (q->n >= q->cap) {
		if (!q->lost++)
			q->overflow_tsc = now;
		return;
	}
	row = &q->bin_ccnt[q->n * (uint64_t)q->nqps];
	for (i = 0; i < q->nqps; i++)
		row[i] = ccnt[i];
	q->bin_tsc[q->n++] = now;

	q->next_tsc += q->period_cyc;
	if (q->next_tsc < now)
		q->next_tsc = now + q->period_cyc;
}

#endif /* PERFTEST_QPTRACE_H */
