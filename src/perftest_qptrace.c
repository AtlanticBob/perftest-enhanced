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
/* perftest_qptrace: per-QP bandwidth-over-time tracing. See the header for
 * the rationale, the two record modes and the resolution caveats. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "perftest_parameters.h"
#include "perftest_resources.h"
#include "perftest_qptrace.h"

struct qptrace_state qptrace = { .mode = QPTRACE_OFF };

static uint64_t realtime_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_REALTIME, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

int qptrace_prepare(struct pingpong_context *ctx,
		    struct perftest_parameters *user_param, int num_of_qps)
{
	struct qptrace_state *q = &qptrace;
	uint64_t budget, row_bytes;
	int i;

	if (!user_param->report_per_qp)
		return 0;

	if (q->mode != QPTRACE_OFF) {
		/* RUN_ALL calls the bw loop once per size; force_dependencies
		 * rejects that combination, so reaching here is a bug. */
		fprintf(stderr, "qptrace: already initialised\n");
		return FAILURE;
	}

	memset(q, 0, sizeof(*q));
	q->nqps = num_of_qps;
	q->msg_size = user_param->size;
	q->cq_mod = user_param->cq_mod;
	q->margin = user_param->margin;
	q->duration = user_param->duration;
	q->csv_path = user_param->report_csv_file_name;
	q->up = user_param;
	q->test_type = user_param->test_type;
	q->duplex = user_param->duplex;
	q->mhz = get_cpu_mhz(user_param->cpu_freq_f);
	if (q->mhz <= 0) {
		fprintf(stderr, "qptrace: could not acquire cpu frequency; "
			"timestamps would be meaningless\n");
		return FAILURE;
	}

	budget = (uint64_t)user_param->report_trace_mb * 1024ull * 1024ull;

	if (user_param->report_interval_us > 0) {
		q->mode = QPTRACE_BIN;
		row_bytes = sizeof(uint64_t) * (1ull + (uint64_t)num_of_qps);
		q->cap = budget / row_bytes;
		q->bin_tsc = calloc(q->cap, sizeof(uint64_t));
		q->bin_ccnt = calloc(q->cap * (uint64_t)num_of_qps,
				     sizeof(uint64_t));
		if (!q->bin_tsc || !q->bin_ccnt) {
			fprintf(stderr, "qptrace: out of memory for %lu bins\n",
				(unsigned long)q->cap);
			return FAILURE;
		}
		q->period_cyc = (uint64_t)(q->mhz *
					   user_param->report_interval_us);
	} else {
		q->mode = QPTRACE_EVENT;
		q->cap = budget / sizeof(struct qptrace_evt);
		q->evt = calloc(q->cap, sizeof(struct qptrace_evt));
		if (!q->evt) {
			fprintf(stderr, "qptrace: out of memory for %lu "
				"events\n", (unsigned long)q->cap);
			return FAILURE;
		}
	}

	/* The join key for the per-QP retransmit counters, which are indexed
	 * by LQPN (see tools/qpstat.py). */
	q->qpn = calloc(num_of_qps, sizeof(uint32_t));
	if (!q->qpn)
		return FAILURE;
	for (i = 0; i < num_of_qps; i++)
		q->qpn[i] = ctx->qp[i] ? ctx->qp[i]->qp_num : 0;

	/* Touch every page now: a first-touch fault inside the polling loop
	 * is exactly the perturbation this module promises not to cause. */
	if (q->evt)
		memset(q->evt, 0, q->cap * sizeof(struct qptrace_evt));
	if (q->bin_ccnt)
		memset(q->bin_ccnt, 0,
		       q->cap * (uint64_t)num_of_qps * sizeof(uint64_t));

	return SUCCESS;
}

void qptrace_start(void)
{
	struct qptrace_state *q = &qptrace;

	if (q->mode == QPTRACE_OFF)
		return;

	q->t0_real_ns = realtime_ns();
	q->t0_tsc = get_cycles();
	q->next_tsc = q->t0_tsc + q->period_cyc;
}

static void qptrace_write_header(FILE *f)
{
	struct qptrace_state *q = &qptrace;
	int i;

	fprintf(f, "# perftest-enhanced qptrace v1\n");
	fprintf(f, "# mode=%s\n",
		q->mode == QPTRACE_EVENT ? "event" : "bin");
	fprintf(f, "# num_of_qps=%d msg_size=%lu cq_mod=%d duplex=%d\n",
		q->nqps, (unsigned long)q->msg_size, q->cq_mod, q->duplex);
	if (q->duplex)
		fprintf(f, "# NOTE duplex: this trace is THIS endpoint's send"
			" completions only; perftest's reported figure adds the"
			" remote endpoint's. The two are not comparable.\n");
	fprintf(f, "# cpu_mhz=%.6f t0_tsc=%llu t0_realtime_ns=%llu\n",
		q->mhz, (unsigned long long)q->t0_tsc,
		(unsigned long long)q->t0_real_ns);
	fprintf(f, "# margin_s=%d duration_s=%d\n", q->margin, q->duration);
	/* The window perftest's own average actually covers. catch_alarm()
	 * stamps these in the same clock as the trace, so this is exact;
	 * [margin, duration - margin] is only a nominal fallback, and on a
	 * flow whose rate steps near an edge the two disagree by percent. */
	if (q->test_type == DURATION && q->up->tposted && q->up->tcompleted &&
	    q->up->tposted[0] > q->t0_tsc &&
	    q->up->tcompleted[0] > q->up->tposted[0])
		fprintf(f, "# sample_start_us=%.3f sample_end_us=%.3f\n",
			(q->up->tposted[0] - q->t0_tsc) / q->mhz,
			(q->up->tcompleted[0] - q->t0_tsc) / q->mhz);
	for (i = 0; i < q->nqps; i++)
		fprintf(f, "# qpn %d %u\n", i, q->qpn[i]);
	if (q->lost)
		fprintf(f, "# TRUNCATED lost=%llu at t_us=%.3f\n",
			(unsigned long long)q->lost,
			(q->overflow_tsc - q->t0_tsc) / q->mhz);
	if (q->mode == QPTRACE_EVENT)
		fprintf(f, "t_us,qp,msgs\n");
	else {
		fprintf(f, "t_us");
		for (i = 0; i < q->nqps; i++)
			fprintf(f, ",qp%d", i);
		fprintf(f, "\n");
	}
}

/* The check that actually matters, and the reason it lives here rather than
 * in force_dependencies(): a per-QP series is only a curve if each bin holds
 * enough completions, and that depends on the rate the run ACHIEVED, which
 * nothing knows before the run. Reported unconditionally, warned about when
 * the finest useful bin turns out to be coarser than the user asked for. */
static void qptrace_report_resolution(void)
{
	struct qptrace_state *q = &qptrace;
	double span_us, evt_per_qp_per_s, usable_bin_us, bin_us;
	uint64_t last_tsc;

	if (!q->n || !q->nqps)
		return;
	last_tsc = q->mode == QPTRACE_EVENT ? q->evt[q->n - 1].tsc
					    : q->bin_tsc[q->n - 1];
	span_us = (last_tsc - q->t0_tsc) / q->mhz;
	if (span_us <= 0)
		return;

	/* In binned mode q->n counts bins, not completions; the completion
	 * count is the last cumulative ccnt summed over QPs. */
	if (q->mode == QPTRACE_EVENT) {
		evt_per_qp_per_s = q->n / (double)q->nqps / (span_us / 1e6);
	} else {
		const uint64_t *row = &q->bin_ccnt[(q->n - 1) * (uint64_t)q->nqps];
		uint64_t tot = 0;
		int i;

		for (i = 0; i < q->nqps; i++)
			tot += row[i];
		evt_per_qp_per_s = tot / (double)q->cq_mod / (double)q->nqps
				   / (span_us / 1e6);
	}
	if (evt_per_qp_per_s <= 0)
		return;

	usable_bin_us = QPTRACE_MIN_EVENTS_PER_BIN / evt_per_qp_per_s * 1e6;
	printf(" qptrace: %.0f completions/s/QP -> finest honest bin is"
	       " ~%.0f us (>= %d completions per QP per bin)\n",
	       evt_per_qp_per_s, usable_bin_us, QPTRACE_MIN_EVENTS_PER_BIN);

	bin_us = q->mode == QPTRACE_BIN ? q->period_cyc / q->mhz : 1000.0;
	if (usable_bin_us > bin_us)
		fprintf(stderr, " qptrace: WARNING at this rate a %.0f us bin"
			" holds only %.2f completions per QP. Do not plot the"
			" per-QP series that fine - it is quantisation noise,"
			" not a curve. Use a coarser bin, or shrink -s (and"
			" then pass -Q 1).\n",
			bin_us, evt_per_qp_per_s * bin_us / 1e6);
}

void qptrace_dump(void)
{
	struct qptrace_state *q = &qptrace;
	char *buf = NULL;
	uint64_t k;
	int i;
	FILE *f;

	if (q->mode == QPTRACE_OFF)
		return;

	f = fopen(q->csv_path, "w");
	if (!f) {
		fprintf(stderr, "qptrace: cannot open %s\n", q->csv_path);
		goto done;
	}
	/* Millions of rows through unbuffered stdio is minutes, not seconds. */
	buf = malloc(4 << 20);
	if (buf)
		setvbuf(f, buf, _IOFBF, 4 << 20);

	qptrace_write_header(f);

	if (q->mode == QPTRACE_EVENT) {
		for (k = 0; k < q->n; k++)
			fprintf(f, "%.3f,%u,%u\n",
				(q->evt[k].tsc - q->t0_tsc) / q->mhz,
				q->evt[k].qp, q->evt[k].msgs);
	} else {
		for (k = 0; k < q->n; k++) {
			const uint64_t *row =
				&q->bin_ccnt[k * (uint64_t)q->nqps];

			fprintf(f, "%.3f", (q->bin_tsc[k] - q->t0_tsc) / q->mhz);
			for (i = 0; i < q->nqps; i++)
				fprintf(f, ",%llu", (unsigned long long)row[i]);
			fprintf(f, "\n");
		}
	}
	fclose(f);

	printf(" qptrace: %llu %s written to %s\n",
	       (unsigned long long)q->n,
	       q->mode == QPTRACE_EVENT ? "events" : "bins", q->csv_path);
	qptrace_report_resolution();
	if (q->lost)
		fprintf(stderr, " qptrace: WARNING buffer full after %.3f s, "
			"%llu records LOST - the tail of this run is missing. "
			"Raise --report-trace-mb or use --report-interval-us.\n",
			(q->overflow_tsc - q->t0_tsc) / q->mhz / 1e6,
			(unsigned long long)q->lost);

done:
	free(buf);
	free(q->evt);
	free(q->bin_tsc);
	free(q->bin_ccnt);
	free(q->qpn);
	memset(q, 0, sizeof(*q));
	q->mode = QPTRACE_OFF;
}
