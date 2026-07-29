#!/usr/bin/env python3
"""Render the demo figure from the traces run_demo.sh produced.

Two panels, one shared time axis and one shared rate axis - deliberately no
second y-scale anywhere.

  top     what the two tools see of the same run: the reported average (one
          number), a 1 s re-bin (the finest the stock tool could report),
          and a 50 ms re-bin (this fork). All three are the SAME data - the
          coarse views are re-binned from the trace, not separate runs.
  bottom  the dimension the stock tool does not have at all: each QP.

Colours are slots 1 and 2 of the reference categorical palette (blue,
orange), assigned by flow identity and never by rank; the three views of a
flow share its hue and differ by weight, because they are the same entity.

usage: plot_demo.py [out.png]
"""
import collections
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
OUT = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.png")

A_HUE, B_HUE = "#2a78d6", "#eb6834"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#8a8983"
SURFACE, GRID = "#fcfcfb", "#e4e3df"

B_OFFSET = 16.0                  # B starts this many seconds after A


def load(tag):
    path = os.path.join(BASE, "%s_trace.csv" % tag)
    head = open(path).read(3000)
    meta = dict(re.findall(r"(\w+)=([\d.]+)", head))
    ev = []
    for ln in open(path):
        if ln[0] in "#t":
            continue
        t_us, q, n = ln.split(",")
        ev.append((float(t_us) / 1e6, int(q), int(n)))
    return meta, ev


def binned(ev, width, size, qp=None):
    c = collections.Counter()
    for t, q, n in ev:
        if qp is not None and q != qp:
            continue
        c[int(t / width)] += n
    ks = sorted(c)[:-1]         # drop the final partial bin: the run ends
                                # mid-bin and it would draw as a real dip
    return ([k * width for k in ks],
            [c[k] * size * 8 / width / 1e9 for k in ks])


def reported(tag):
    for ln in open(os.path.join(BASE, "%s_perftest.log" % tag)):
        f = ln.split()
        if len(f) >= 4 and f[0].isdigit():
            return float(f[3])
    return None


def jain(xs):
    xs = [x for x in xs if x > 0]
    if not xs:
        return float("nan")
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs))


def main():
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 7.6), sharex=True,
        gridspec_kw={"height_ratios": [1.5, 1], "hspace": 0.34})
    fig.patch.set_facecolor(SURFACE)

    stats = {}
    for tag, hue, off in (("A", A_HUE, 0.0), ("B", B_HUE, B_OFFSET)):
        meta, ev = load(tag)
        size = int(meta["msg_size"])
        margin, dur = float(meta["margin_s"]), float(meta["duration_s"])
        avg = reported(tag)

        # The window perftest's average really covers, as stamped by
        # catch_alarm - not [margin, duration-margin], which is off by
        # however long init took (415 ms on this host).
        win_lo = float(meta.get("sample_start_us", margin * 1e6)) / 1e6
        win_hi = float(meta.get("sample_end_us",
                                (dur - margin) * 1e6)) / 1e6

        t50, v50 = binned(ev, 0.05, size)
        ax1.plot([x + off for x in t50], v50, color=hue, lw=0.9, alpha=0.5,
                 solid_joinstyle="round", zorder=2)
        t1k, v1k = binned(ev, 1.0, size)
        ax1.step([x + off for x in t1k], v1k, where="post", color=hue,
                 lw=2.0, zorder=3)
        ax1.plot([win_lo + off, win_hi + off], [avg, avg],
                 color=hue, lw=2.0, ls=(0, (1, 2)), zorder=4)
        ax1.annotate("%s: perftest reports %.2f" % (tag, avg),
                     xy=(win_hi + off, avg), xytext=(6, 0),
                     textcoords="offset points", va="center",
                     fontsize=9.5, color=hue, fontweight="600")

        # per-QP, one thin line each; they overlap when the executor acts on
        # the flow rather than the QP, which is itself the finding
        nq = int(meta["num_of_qps"])
        series = []
        for q in range(nq):
            tq, vq = binned(ev, 0.05, size, qp=q)
            ax2.plot([x + off for x in tq], vq, color=hue, lw=0.8,
                     alpha=0.55, zorder=2)
            series.append((tq, vq))
        idx = {}
        for tq, vq in series:
            for t, v in zip(tq, vq):
                idx.setdefault(round(t, 3), []).append(v)
        js = [jain(v) for v in idx.values() if len(v) == nq and sum(v) > 1]
        win = sorted(v for t, v in zip(*binned(ev, 0.05, size))
                     if win_lo <= t <= win_hi)
        near = sum(1 for v in win if abs(v - avg) / avg < 0.05)
        stats[tag] = (nq, sum(js) / len(js) if js else float("nan"),
                      100.0 * near / len(win) if win else 0.0, avg,
                      win[len(win) // 10], win[len(win) // 2])

    def head(ax, title, sub):
        ax.set_title(title, color=INK, fontsize=12.5, fontweight="600",
                     loc="left", pad=26)
        ax.text(0.0, 1.017, sub, transform=ax.transAxes, fontsize=9.5,
                color=MUTED, va="bottom")

    ax1.set_ylabel("Gb/s", color=INK2, fontsize=10)
    head(ax1,
         "One number, a 1 s re-bin, and a 50 ms re-bin — all from the same "
         "trace",
         "A held its own reported average for %.1f%% of its sample window "
         "(p10 %.1f, median %.1f Gb/s — the average lands in the gap between "
         "the two modes)" % (stats["A"][2], stats["A"][4], stats["A"][5]))

    ax2.set_ylabel("Gb/s per QP", color=INK2, fontsize=10)
    ax2.set_xlabel("seconds since flow A started", color=INK2, fontsize=10)
    head(ax2,
         "Per-QP, 50 ms bins — %d QPs per flow, drawn separately"
         % stats["A"][0],
         "The lines coincide: mean Jain across A's QPs %.4f, B's %.4f. The "
         "rate limiter acts on the flow, not the QP."
         % (stats["A"][1], stats["B"][1]))

    for ax, top in ((ax1, 33), (ax2, 15)):
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, lw=0.8, zorder=0)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=9.5, length=0)
        ax.set_xlim(-0.5, 41)
        ax.set_ylim(0, top)

    handles = [
        plt.Line2D([], [], color=A_HUE, lw=2.0, label="flow A  (vf0)"),
        plt.Line2D([], [], color=B_HUE, lw=2.0, label="flow B  (vf3)"),
        plt.Line2D([], [], color=MUTED, lw=0.9, alpha=0.6,
                   label="50 ms re-bin"),
        plt.Line2D([], [], color=MUTED, lw=2.0, label="1 s re-bin"),
        plt.Line2D([], [], color=MUTED, lw=2.0, ls=(0, (1, 2)),
                   label="perftest's reported average"),
    ]
    ax1.legend(handles=handles, loc="lower left", frameon=False, ncol=5,
               fontsize=9, labelcolor=INK2, columnspacing=1.4,
               handlelength=1.8, bbox_to_anchor=(0.005, 0.02))

    fig.savefig(OUT, dpi=160, facecolor=SURFACE, bbox_inches="tight")
    print("wrote %s" % OUT)
    for tag, (nq, j, pct, avg, p10, med) in stats.items():
        print("  %s: %d QPs, reported %.2f Gb/s, held it %.1f%% of the "
              "window, mean Jain %.4f" % (tag, nq, avg, pct, j))


if __name__ == "__main__":
    main()
