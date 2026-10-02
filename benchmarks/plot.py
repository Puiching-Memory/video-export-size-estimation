"""Render diagnostic results without hiding missing or full-encode outputs.

Optional dependency: matplotlib. This is not required by the core package.
"""

import argparse
import json
import statistics
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("evaluated", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = json.loads(args.evaluated.read_text())
    methods = ["uniform", "ab-av1", "visual-residual", "controller", "full-encode"]
    labels = ["Uniform", "ab-av1", "Visual residual", "Controller", "Full export"]
    budgets = sorted({r["budget_seconds"] for r in rows})
    colors = ["#126782", "#83c5be", "#e9a458", "#dde1e5"]
    figure, axes = plt.subplots(1, len(budgets), figsize=(13, 4.4), sharey=True)
    for axis, budget in zip(axes, budgets):
        for index, method in enumerate(methods):
            group = [r for r in rows if r["budget_seconds"] == budget and r["method"] == method]
            exact = sum(r.get("evidence") == "exact" for r in group)
            good = sum(r.get("absolute_error_percent", float("inf")) <= 10 for r in group)
            outputs = sum(r["prediction_bytes"] is not None for r in group)
            counts = [exact, good - exact, outputs - good, len(group) - outputs]
            left = 0
            for count, color in zip(counts, colors):
                axis.barh(index, count, left=left, color=color, height=0.65)
                if count:
                    axis.text(
                        left + count / 2,
                        index,
                        str(count),
                        ha="center",
                        va="center",
                        color="white" if color == colors[0] else "#263238",
                        fontsize=10,
                    )
                left += count
            elapsed = statistics.median(r["wall_seconds"] for r in group)
            axis.text(9.25, index, f"{elapsed:.2f}s", va="center", fontsize=9, color="#455a64")
        axis.set_title(f"{budget:g} second budget", fontsize=12, loc="left")
        axis.set_yticks(range(len(labels)), labels)
        axis.set_xticks([0, 3, 6, 9])
        axis.set_xlim(0, 11.5)
        axis.set_xlabel("Cases out of 9; median runtime at right", fontsize=9)
        axis.spines[["top", "right", "left"]].set_visible(False)
        axis.tick_params(axis="y", length=0)
    axes[0].invert_yaxis()
    figure.suptitle(
        "Development diagnostics: accuracy, exact work, and missing results",
        x=0.12,
        ha="left",
        fontsize=14,
    )
    names = ["Exact full export", "Estimate within 10%", "Estimate outside 10%", "No estimate"]
    figure.legend(
        [Patch(color=c) for c in colors],
        names,
        ncol=4,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.015),
        frameon=False,
        fontsize=10,
    )
    figure.text(
        0.12,
        0.095,
        "7 synthetic + 2 public clips; cold cache; single run. Not a SOTA benchmark.",
        fontsize=9,
        color="#455a64",
    )
    figure.subplots_adjust(left=0.12, right=0.98, bottom=0.24, top=0.84, wspace=0.2)
    figure.savefig(args.output, dpi=180)
    vector = args.output.with_suffix(".svg")
    figure.savefig(vector)
    vector.write_text("\n".join(line.rstrip() for line in vector.read_text().splitlines()) + "\n")
    plt.close(figure)


if __name__ == "__main__":
    main()
