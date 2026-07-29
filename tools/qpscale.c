/* qpscale: how much does QP setup parallelise inside ONE ibv context?
 *
 * perftest spends its setup time in two per-QP loops - ibv_create_qp
 * (measured 53% of setup) and the three ibv_modify_qp transitions (31%).
 * Four separate perftest PROCESSES set up 16384 QPs in 19.8 s against 51.5 s
 * for one process, so the work is not hardware-serialised. But four
 * processes are four ibv contexts; threads inside one context may contend
 * on locks that separate contexts do not share. That difference decides
 * whether threading perftest's loops is worth doing, so measure it.
 *
 * Same shape as perftest: one context, one PD, one CQ shared by all QPs,
 * RC, and the same INIT -> RTR -> RTS sequence. Each QP is pointed at
 * itself for RTR, which exercises the same syscall without needing a peer.
 *
 * This is the measurement that justified --setup-threads, kept as the
 * regression check for it: if a future kernel or libibverbs serialises the
 * setup path again, the speedup here collapses and so will perftest's.
 *
 * build: gcc -O2 -o qpscale qpscale.c -libverbs -lpthread
 * usage: qpscale <ibdev> <gid_index> <num_qps> <threads>
 *
 * Reference numbers, mlx5, 16384 QPs:
 *   1 thread   create  9.20 s   modify 15.55 s   total 24.75 s
 *   8 threads  create  1.67 s   modify  2.41 s   total  4.08 s   (6.1x)
 */
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <infiniband/verbs.h>

static struct ibv_context *ctx;
static struct ibv_pd *pd;
static struct ibv_cq *cq;
static struct ibv_qp **qps;
static union ibv_gid gid;
static int gid_index, nqp, nthr, port = 1;

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

struct span { int lo, hi; };

static void *do_create(void *arg)
{
	struct span *s = arg;
	struct ibv_qp_init_attr a;
	int i;

	for (i = s->lo; i < s->hi; i++) {
		memset(&a, 0, sizeof a);
		a.send_cq = cq;
		a.recv_cq = cq;
		a.qp_type = IBV_QPT_RC;
		a.cap.max_send_wr = 4;
		a.cap.max_recv_wr = 4;
		a.cap.max_send_sge = 1;
		a.cap.max_recv_sge = 1;
		qps[i] = ibv_create_qp(pd, &a);
		if (!qps[i]) {
			fprintf(stderr, "create_qp %d: %s\n", i, strerror(errno));
			return (void *)1;
		}
	}
	return NULL;
}

static void *do_modify(void *arg)
{
	struct span *s = arg;
	struct ibv_qp_attr a;
	int i, rc;

	for (i = s->lo; i < s->hi; i++) {
		memset(&a, 0, sizeof a);
		a.qp_state = IBV_QPS_INIT;
		a.pkey_index = 0;
		a.port_num = port;
		a.qp_access_flags = IBV_ACCESS_REMOTE_WRITE |
				    IBV_ACCESS_LOCAL_WRITE;
		rc = ibv_modify_qp(qps[i], &a, IBV_QP_STATE | IBV_QP_PKEY_INDEX |
				   IBV_QP_PORT | IBV_QP_ACCESS_FLAGS);
		if (rc) { fprintf(stderr, "INIT %d: %s\n", i, strerror(rc)); return (void *)1; }

		memset(&a, 0, sizeof a);
		a.qp_state = IBV_QPS_RTR;
		a.path_mtu = IBV_MTU_1024;
		a.dest_qp_num = qps[i]->qp_num;      /* itself: same syscall */
		a.rq_psn = 0;
		a.max_dest_rd_atomic = 1;
		a.min_rnr_timer = 12;
		a.ah_attr.is_global = 1;
		a.ah_attr.grh.dgid = gid;
		a.ah_attr.grh.sgid_index = gid_index;
		a.ah_attr.grh.hop_limit = 1;
		a.ah_attr.dlid = 0;
		a.ah_attr.sl = 0;
		a.ah_attr.src_path_bits = 0;
		a.ah_attr.port_num = port;
		rc = ibv_modify_qp(qps[i], &a, IBV_QP_STATE | IBV_QP_AV |
				   IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
				   IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC |
				   IBV_QP_MIN_RNR_TIMER);
		if (rc) { fprintf(stderr, "RTR %d: %s\n", i, strerror(rc)); return (void *)1; }

		memset(&a, 0, sizeof a);
		a.qp_state = IBV_QPS_RTS;
		a.timeout = 14;
		a.retry_cnt = 7;
		a.rnr_retry = 7;
		a.sq_psn = 0;
		a.max_rd_atomic = 1;
		rc = ibv_modify_qp(qps[i], &a, IBV_QP_STATE | IBV_QP_TIMEOUT |
				   IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY |
				   IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC);
		if (rc) { fprintf(stderr, "RTS %d: %s\n", i, strerror(rc)); return (void *)1; }
	}
	return NULL;
}

static double run(void *(*fn)(void *))
{
	pthread_t th[64];
	struct span sp[64];
	int t, per = (nqp + nthr - 1) / nthr;
	double t0 = now();

	for (t = 0; t < nthr; t++) {
		sp[t].lo = t * per;
		sp[t].hi = (t + 1) * per > nqp ? nqp : (t + 1) * per;
		pthread_create(&th[t], NULL, fn, &sp[t]);
	}
	for (t = 0; t < nthr; t++)
		pthread_join(th[t], NULL);
	return now() - t0;
}

int main(int argc, char **argv)
{
	struct ibv_device **list;
	struct ibv_device *dev = NULL;
	double tc, tm;
	int i, n = 0;

	if (argc < 5) {
		fprintf(stderr, "usage: %s <ibdev> <gid_index> <num_qps> <threads>\n", argv[0]);
		return 2;
	}
	gid_index = atoi(argv[2]);
	nqp = atoi(argv[3]);
	nthr = atoi(argv[4]);
	if (nthr > 64) nthr = 64;

	list = ibv_get_device_list(&n);
	for (i = 0; i < n; i++)
		if (!strcmp(ibv_get_device_name(list[i]), argv[1]))
			dev = list[i];
	if (!dev) { fprintf(stderr, "no device %s\n", argv[1]); return 2; }

	ctx = ibv_open_device(dev);
	if (!ctx) { perror("open_device"); return 2; }
	if (ibv_query_gid(ctx, port, gid_index, &gid)) { perror("query_gid"); return 2; }
	pd = ibv_alloc_pd(ctx);
	cq = ibv_create_cq(ctx, nqp * 4 + 16, NULL, NULL, 0);
	if (!pd || !cq) { fprintf(stderr, "pd/cq alloc failed\n"); return 2; }

	qps = calloc(nqp, sizeof *qps);
	if (!qps) return 2;

	tc = run(do_create);
	tm = run(do_modify);

	printf("%-8s qps=%-6d threads=%-3d  create %6.3f s (%5.0f us/qp)  "
	       "modify %6.3f s (%5.0f us/qp)  total %6.3f s\n",
	       argv[1], nqp, nthr, tc, tc / nqp * 1e6, tm, tm / nqp * 1e6, tc + tm);
	return 0;
}
