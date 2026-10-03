# Native export size and quality after holdout scoring

On the nine previously scored native 130-frame sequences, independent 0.5-second
segmentation changes file size by a median **+4.57%**, with a maximum **+236.84%**.
Average PSNR against the original source changes by a median **−0.138 dB**;
SSIM changes by a median **−0.000609**. The two screen cases become substantially
larger while scoring lower on both measured signal metrics. These measurements
characterize the cost of changing export semantics. They do not change the
already-scored prediction policy or establish perceptual acceptance.

## All nine results

Both outputs are the actual completed CRF 23, medium, native-resolution,
original-frame-rate references. Every source and output contains 130 frames.
File change is `(segmented / continuous) − 1`; quality deltas are
`segmented − continuous`. PSNR values are FFmpeg's aggregate full-YUV values,
not the arithmetic mean of individual frame PSNR values.

| Case | Native dimensions | File change | Continuous PSNR | Segmented PSNR | PSNR delta | Continuous SSIM | Segmented SSIM | SSIM delta |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| scene-composition | 1920×1080 | +236.84% | 46.604 dB | 44.092 dB | −2.513 dB | 0.995770 | 0.993329 | −0.002441 |
| mobile-sharing | 1078×2220 | +205.14% | 47.260 dB | 44.573 dB | −2.687 dB | 0.999148 | 0.998648 | −0.000500 |
| artistic-intro | 1920×1080 | +4.57% | 39.492 dB | 39.331 dB | −0.161 dB | 0.973898 | 0.973288 | −0.000610 |
| noise-ocean-game | 1920×1080 | −9.59% | 44.558 dB | 43.765 dB | −0.794 dB | 0.973503 | 0.967810 | −0.005693 |
| shaky-baseball-4k | 3840×2160 | +6.67% | 42.882 dB | 42.821 dB | −0.061 dB | 0.974279 | 0.974260 | −0.000019 |
| shaky-fireworks-4k | 3840×2160 | +3.60% | 50.666 dB | 50.818 dB | +0.152 dB | 0.993732 | 0.993939 | +0.000207 |
| shaky-walk | 1920×1080 | +4.03% | 40.217 dB | 40.265 dB | +0.048 dB | 0.978992 | 0.978976 | −0.000016 |
| trees-grass | 1920×1080 | +10.08% | 34.151 dB | 34.013 dB | −0.138 dB | 0.952681 | 0.951659 | −0.001022 |
| walking-street | 1080×1920 | +3.93% | 35.153 dB | 35.064 dB | −0.090 dB | 0.931685 | 0.931076 | −0.000609 |

The file-change range is −9.59% to +236.84%. PSNR deltas range from −2.687 dB
to +0.152 dB; SSIM deltas range from −0.005693 to +0.000207. Seven cases have
lower segmented PSNR and eight have lower segmented SSIM. The signal-quality
direction can differ between metrics, as shaky-walk illustrates.

## Identity, frame pairing, and real measurements

The script reads only the already-completed reference index and the locked
source manifest for this post-score characterization. It does not open
prediction-accuracy results, run predictions, re-encode videos, or alter any
source policy/encoder setting.

Before measuring, it verifies SHA-256 of all nine original Y4M sources and all
18 actual MP4 references. It checks that both output source hashes match the
same locked source, that recorded reference settings are native CRF 23 medium
with one thread, and that actual dimensions, eight-bit YUV420 pixel format and
frame rate match. Source Y4M frame records are counted directly, including
header/truncation checks. Actual compressed output packet counts must be 130,
and the references' earlier complete decode inspections must also report 130.
The metric runs then freshly decode every frame of all three inputs.

One FFmpeg process per case decodes source, continuous output, and segmented
output. Each decoder uses one thread, and both filter thread settings are one.
The source splits into four metric branches, so both outputs are compared to
identical source pixels. Each decoded presentation-order frame is assigned the
same frame-index-derived clock at the verified original rate. There is no
spatial resizing, frame skipping, or conversion to an RGB display space.
PSNR/SSIM framesync uses `shortest=1` and `repeatlast=0` to prohibit silently
repeating the final reference frame.

All **36** metric-stat files contain exactly frame numbers 1 through 130.
Named filter logs identify the continuous and segmented aggregate independently.
The aggregate PSNR is cross-checked against the mean per-frame MSE, using the
known two-decimal MSE quantization interval. Aggregate SSIM is cross-checked
against the per-frame SSIM mean, allowing only the printed six-decimal rounding.
These checks cover all 18 output/reference comparisons (`verification.json`).

Each case's original commands, FFmpeg logs, source/output identity checks,
per-frame PSNR/SSIM statistics, and metric summaries are retained under `cases/`.
These are real FFmpeg measurements, with no synthetic score substitution.

## Interpretation and limits

This FFmpeg build has **no libvmaf filter**; `ffmpeg-filters.txt` and
`metadata.json` record that fact. VMAF was not measured. There was no subjective
viewing, temporal-flicker assessment, display-specific evaluation, or perceptual
acceptance threshold. PSNR measures pixel error and SSIM measures local
structural similarity; neither supplies industrial perceptual certification.

These nine sequences are short, eight-bit SDR clips at their original dimensions
and rates. They do not establish native long-video behavior, HDR performance,
or behavior at other CRFs/presets/segment lengths. They also do not constitute an
equal-bitrate rate-distortion comparison: both targets use the same requested
CRF, while their actual size and encoder state differ.

The screen measurements show why the independent-segment target cannot simply
inherit continuous-export quality/size expectations. For scene-composition,
3.37 times the file size accompanies a 2.513 dB PSNR reduction; for
mobile-sharing, 3.05 times the size accompanies a 2.687 dB reduction. These are
observed tradeoffs at the frozen settings, not parameters chosen to improve a
holdout score. Prediction accuracy for the segmented contract and its output
quality remain separate questions.

## Resource accounting and reproduction

The long complete reference and decode had finished before this run started.
Metric jobs were serial, ran at `nice=10`, and checked for other active FFmpeg
processes before starting each case. Other cloud/API/development work could
still run; the timings are **not isolated latency measurements**.

Source/artifact SHA and metadata verification took 13.47 s in total; the actual
PSNR/SSIM processes took 25.47 s. This separate analysis is explicitly **not
charged to the earlier controller budget**. It produces only text/JSON/CSV
statistics and null video output, with no new MP4/raw artifacts.

Reproduce from the repository root after the completed references exist:

```bash
.venv/bin/python benchmarks/industrial_quality.py
```

The case allowlist contains only these nine already-scored native short cases.
`--cases` permits a subset for recovery, without allowing new cases.
`--output` can direct an independent repeat to a new directory. Reproduction
rehashes sources and references and runs actual metrics again; it does not
reuse a cached quality score.

Machine-readable results: `results.json`, `summary.json`, `comparison.csv`,
`metadata.json`, `verification.json`, and per-case evidence in `cases/`.
