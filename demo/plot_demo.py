#!/usr/bin/env python3
"""Instantaneous per-flow bandwidth, from the traces run_demo.sh produced.

One panel, one y-scale, two lines. Nothing else.

Smoothing: a SLIDING window, not wider bins. With disjoint bins, asking for
a smoother line means asking for fewer points, and the result turns into a
staircase. A sliding window separates the two knobs - the window sets how
much data each point averages (smoothness), the step sets how many points
there are (density). Here: a 100 ms window advanced every 5 ms, so 200
points per second whatever the window is.

100 ms measured on a genuinely flat stretch of this run: adjacent points
differ by 0.30% on average, 1.6% at worst. At a 10 ms window that is 2.73%
/ 14.2% - visibly ragged. At 500 ms it is smoother still but starts
rounding off transitions. The events here (a flow entering, congestion
control reacting) take seconds, so 100 ms costs nothing that matters.

usage: plot_demo.py [out.png]
"""
import bisect
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
OUT = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.png")

WINDOW, STEP = 0.100, 0.005
PLOT_FROM = 0.0           # the whole run. If the sender-side limiter takes
                          # its one-off early excursion (README section 4)
                          # it will be visible here, and should be.

A_HUE, B_HUE = "#2a78d6", "#eb6834"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#8a8983"
SURFACE, GRID, BAND = "#fcfcfb", "#e4e3df", "#f0efec"


def load(tag):
    path = os.path.join(BASE, "%s_trace.csv" % tag)
    meta = dict(re.findall(r"(\w+)=([\d.]+)", open(path).read(3000)))
    ts = []
    for ln in open(path):
        if ln[0] in "#t":
            continue
        ts.append(float(ln.split(",", 1)[0]) / 1e6)
    ts.sort()
    return meta, ts


def sliding(ts, offset, lo, hi, size):
    """Rate at every STEP, each point averaging WINDOW of completions."""
    xs, ys = [], []
    x = lo
    while x <= hi:
        n = (bisect.bisect_right(ts, x - offset + WINDOW / 2) -
             bisect.bisect_left(ts, x - offset - WINDOW / 2))
        xs.append(x)
        ys.append(n * size * 8 / WINDOW / 1e9)
        x += STEP
    return xs, ys


def main():
    meta_a, ts_a = load("A")
    meta_b, ts_b = load("B")
    size = int(meta_a["msg_size"])

    # B's offset in A's clock comes from the traces themselves (both headers
    # carry t0_realtime_ns), not from the sleep in the run script.
    offset = (int(meta_b["t0_realtime_ns"]) -
              int(meta_a["t0_realtime_ns"])) / 1e9
    b_in, b_out = offset + ts_b[0], offset + ts_b[-1]
    a_end = ts_a[-1]

    fig, ax = plt.subplots(figsize=(11, 5.0))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ax.axvspan(b_in, b_out, color=BAND, zorder=0)
    for x, label, side in ((b_in, "B enters", 1), (b_out, "B leaves", -1)):
        ax.axvline(x, color=MUTED, lw=1.2, ls=(0, (4, 3)), zorder=1)
        ax.annotate("%s\nt = %.1f s" % (label, x), xy=(x, 24.0),
                    xytext=(7 * side, 0), textcoords="offset points",
                    ha="left" if side > 0 else "right", va="top",
                    fontsize=9.5, color=INK2, linespacing=1.5)

    for name, ts, off, hue, lo, hi in (
            ("flow A  (vf0)", ts_a, 0.0, A_HUE, PLOT_FROM, a_end),
            ("flow B  (vf3)", ts_b, offset, B_HUE, b_in, b_out)):
        xs, ys = sliding(ts, off, lo + WINDOW / 2, hi - WINDOW / 2, size)
        ax.plot(xs, ys, color=hue, lw=2.0, label=name,
                solid_capstyle="round", solid_joinstyle="round", zorder=3)

    for x, t in ((0.5 * (PLOT_FROM + b_in), "A alone"),
                 (0.5 * (b_in + b_out), "A and B"),
                 (0.5 * (b_out + a_end), "A alone")):
        ax.text(x, 33.6, t, ha="center", va="bottom", fontsize=10.5,
                color=INK2, fontweight="600")

    ax.set_ylabel("Gb/s", color=INK2, fontsize=10.5)
    ax.set_xlabel("seconds since flow A started", color=INK2, fontsize=10.5)
    ax.set_title("Instantaneous bandwidth per flow",
                 color=INK, fontsize=13, fontweight="600", loc="left",
                 pad=36)
    ax.text(0.0, 1.05,
            "100 ms sliding window advanced every 5 ms — 200 points per "
            "second. 4 QPs and 65536 B messages per flow, both aimed at the "
            "same destination VF.",
            transform=ax.transAxes, fontsize=9.5, color=MUTED, va="bottom")

    ax.grid(True, color=GRID, lw=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=10, length=0)
    ax.set_xlim(PLOT_FROM - 0.5, a_end + 0.5)
    ax.set_ylim(0, 37)
    ax.set_yticks([0, 10, 20, 30])
    ax.legend(loc="lower left", frameon=False, fontsize=10, labelcolor=INK2,
              ncol=2, handlelength=1.8, bbox_to_anchor=(0.005, 0.01))

    fig.savefig(OUT, dpi=160, facecolor=SURFACE, bbox_inches="tight")
    print("wrote %s" % OUT)
    print("  B in %.2f s, out %.2f s, A ends %.2f s" % (b_in, b_out, a_end))


if __name__ == "__main__":
    main()
