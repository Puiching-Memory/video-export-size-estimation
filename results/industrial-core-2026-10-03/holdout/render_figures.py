"""Render measured holdout outcomes; this script does not encode or fit models."""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

root = Path(__file__).resolve().parent
content = json.loads((root / 'summary.json').read_text())
temporal = json.loads((root / 'temporal-summary.json').read_text())
official = json.loads((root / 'baseline-summary.json').read_text())
fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
for row, mode in enumerate(['continuous', 'segmented']):
    values = [next(v for v in content if v['mode'] == mode and v['max_encode_fraction'] == .2),
              next(v for v in temporal if v['mode'] == mode),
              next(v for v in content if v['mode'] == mode and v['max_encode_fraction'] == .5)]
    labels = ['Content 20%', 'Temporal 20%', 'Content 50%']
    colors = ['#2166ac', '#d97706', '#67a9cf']
    if mode == 'continuous':
        values.append(official)
        labels.append('ab-av1\n3 × 0.5 s')
        colors.append('#7b3294')
    x = np.arange(len(values))
    ax = axes[row, 0]
    for i, value in enumerate(values):
        ax.scatter(i, value['median_ape_percent'], color=colors[i], marker='o', s=70,
                   label='Median' if i == 0 else None)
        ax.scatter(i, value['worst_ape_percent'], color=colors[i], marker='^', s=65,
                   label='Worst' if i == 0 else None)
    ax.set_yscale('log')
    ax.set_ylim(.5, 500)
    ax.axhline(10, color='#666666', linestyle='--', linewidth=1)
    ax.set_xticks(x, labels)
    ax.set_ylabel('Absolute relative error (%) — log scale')
    ax.set_title(mode.capitalize() + ': errors among available estimates')
    ax.grid(axis='y', alpha=.25)
    ax.legend(loc='upper left', frameon=False)
    ax = axes[row, 1]
    percentages = [100 * v['within_10_percent_all_cases'] for v in values]
    ax.bar(x, percentages, color=colors)
    for i, value in enumerate(values):
        ax.text(i, percentages[i] + 3, f"{value['estimates']}/9 estimates", ha='center', fontsize=9)
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 100)
    ax.set_ylabel('Within 10% / all 9 cases (%)')
    ax.set_title(mode.capitalize() + ': failures stay in denominator')
    ax.grid(axis='y', alpha=.25)
fig.suptitle('Frozen native AOM-source holdout · 9 clips / 8 source groups · 15 s, 1 thread\n'
             'Each target has its own full reference; all sampling outputs remain uncalibrated', fontsize=12)
fig.savefig(root / 'holdout-outcomes.svg')
fig.savefig(root / 'holdout-outcomes.png', dpi=180)
plt.close(fig)
