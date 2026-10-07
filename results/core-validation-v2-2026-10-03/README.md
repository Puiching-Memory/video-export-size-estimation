# Core development validation: policy v2

Content discovery substantially reduces the missed-burst error for explicit segmented exports: with a 20% attempted-video budget, all nine development sources produced estimates, eight were within 10%, median absolute percentage error was 0.621%, and the worst was 13.775%. This is an improvement on the v1 failures, not industrial acceptance or SOTA. The original continuous-export target still has large startup/context errors, and one-second segmentation carries a material file-size and quality cost.

## Fixed experiment and evidence

The same nine already-opened development sources were used, with no industrial calibration or held-out accuracy read for this work. Each uses libx264 medium, CRF 23, yuv420p, one encoder/input/filter thread, no audio and no scaling or FPS conversion. Continuous probes are one second; the explicit independently encoded segmented target also uses one-second segments. These are different targets, each scored against its own fresh completed export. All controller predictions were persisted before those labels were generated/read.

The new policy uses a four-FPS 96×54 decoded preview for content strata, a budget-matched requested pilot count, randomized residual audits, automatic context/bare selection, and a video-only MP4 table-based container model. Discovery still decodes the source and is charged to wall time. The encoded-video ledger charges requested output-video spans, including complete padding spans and attempted work that is later cancelled; it is not a CPU-cost estimate and does not account for discovery as encoded video. Actual audio cost is a separate field, but these sources have no audio.

The controller experiment began with source SHA-256 `3e493949d0973f3671815c96675e8b515f25a6e32566d1744d433ce94f33a9b1`. Root subsequently finished formatting and malformed-cache robustness changes, then froze source SHA-256 `f39dca1c9f04afec908f1171daa898e2bf4c3fa82073cdb5908d8b9eec562ee2`. No accuracy policy was retuned during this experiment. The ab-av1, fallback and quality stages ran in fresh processes on the final frozen source. This is explicitly a staged development experiment rather than one immutable-commit measurement. Every stage saves its actual code/runner snapshot under ignored `.vsize-cache/code_snapshot`, with hashes in `metadata.json`. The engine snapshot hash was independently verified against its metadata.

All jobs ran in the shared four-CPU cloud cgroup, one FFmpeg at a time within this runner; the native evaluation agent sometimes ran another one-thread FFmpeg concurrently. Timings are not claimed to be isolated or randomized speed measurements. A fresh application cache was used per controller request. The OS page cache was not flushed. Prepared-input requests reuse an immutable sealed source snapshot, not an estimator/feature cache; ingestion cost is reported outside the warm request and also included in a separate end-to-end field.

## Five-second controller requests

There were 72 requests: nine sources × two modes × two video budgets × original/prepared input. Each allowed at most six new probes. All produced an estimate, no charged video fraction exceeded its request, and no uncalibrated result was falsely marked `satisfied`. The largest observed five-second cancellation overrun was 13.51 ms.

| Mode, original input | Video budget | Within 10%, all nine cases | Median APE | Worst APE | Median charged video fraction | Median wall | Median first estimate |
|---|---:|---:|---:|---:|---:|---:|---:|
| Continuous | 20% | 4/9 | 14.158% | 135.879% | 16.67% | 1.618 s | 0.735 s |
| Continuous | 50% | 5/9 | 9.852% | 135.879% | 25.00% | 2.054 s | 0.707 s |
| Segmented, 1s | 20% | 8/9 | 0.621% | 13.775% | 16.67% | 1.979 s | 0.799 s |
| Segmented, 1s | 50% | 8/9 | 0.633% | 13.775% | 25.00% | 2.324 s | 0.781 s |

The 50% setting did not imply spending 50%: the six-probe limit often stopped work around 25%. Prepared-input accuracy was nearly identical; deadlines changed the available work for a few runs. Original-input median discovery cost was 0.291–0.340 s across these groups, with worst 1.796 s on grain. All feature-cache hits were false, as required by the fresh per-request cache setup. Full detail is in `engine/{continuous,segmented}/summary.json`, `engine/evaluated.json`, per-request answers and update streams.

| Source | Continuous 20% signed error | Segmented 20% signed error |
|---|---:|---:|
| static | +91.791% | 0.000% |
| motion | +7.758% | −1.279% |
| grain | −1.228% | −0.405% |
| brief-burst | −1.182% | −1.649% |
| cbr-burst | +14.158% | +13.775% |
| alternating | −3.192% | −0.621% |
| screen | +60.037% | +0.226% |
| pedestrians | +135.879% | +0.171% |
| megamind | +16.953% | −0.812% |

Content discovery repaired the v1 burst misses without removing the CBR-burst counterexample. Continuous automatic admission selected bare probes in these small-budget runs because all requested padded pilots could not fit. Its container correction does not remove startup I/SEI or historical encoder-state bias. The large static, screen and pedestrians errors remain visible; this experiment has not solved continuous low-budget estimation.

## ab-av1 comparison at one-second samples

ab-av1 v0.11.7 used three one-second samples, identical libx264 medium CRF 23 settings, explicit keyint 250, one input/encoder/filter thread, no VMAF and no cache, with a five-second deadline. All nine produced predictions; six were within 10% of the continuous reference, median APE was 6.482%, worst APE was 67.390%, and median wall was 1.513 s. Median actually encoded duration was 12.5% of the source, 7.5% for pedestrians and 27.48% for Megamind. These costs differ from the controller's adaptive four/six probes and its source preview; neither elapsed time nor fraction is silently normalized.

The baseline prediction is used unmodified against the continuous file target, with payload errors reported separately. Its logged “video stream size” has container-dependent ratios, as documented in the v1 report; no favorable overhead correction was invented. The baseline is a comparator for the continuous target, not for the larger one-second segmented target. Exact commands, retained sample durations and errors are in `ab-av1/`.

## One-second segmentation is not a free accuracy improvement

All nine complete one-second segmented references decoded and matched additive byte accounting. PSNR/SSIM were measured against the original decoded source at matching presentation timestamps, using already-generated references. The following costs must accompany any statement about the segmented estimator's improved point accuracy.

| Source | Segmented file-size change versus continuous | PSNR change | SSIM change |
|---|---:|---:|---:|
| static | +116.669% | Both infinite | 0 |
| motion | +9.716% | −0.108 dB | −0.000064 |
| grain | +1.180% | −0.003 dB | +0.000885 |
| brief-burst | +0.912% | −0.057 dB | −0.000079 |
| cbr-burst | +5.688% | −0.229 dB | −0.000309 |
| alternating | −2.479% | −0.405 dB | −0.002143 |
| screen | +63.540% | −0.468 dB | −0.000017 |
| pedestrians | +136.392% | −1.641 dB | −0.005523 |
| megamind | +15.034% | −0.437 dB | −0.000927 |

For comparison, the v1 four-second segmented pedestrians output cost +30.631% and −0.382 dB. Reducing the unit to fit more representatives in a 20% budget increases reset overhead and changes compression behavior. The segmented mode remains an explicit export choice with a tunable segment duration; it cannot be substituted for the original continuous task and called a solution without this tradeoff. Exact values/commands are in `quality/results.json`.

## No-fraction fallback boundary

Eighteen further requests used prepared sources, the same content/auto policy, one-second units, no video-fraction ceiling, at most six probes and a ten-second wall budget. They used fresh application caches and reused the already-verified reference labels only after all predictions were saved.

Continuous mode produced seven exact completed exports and two uncalibrated predictions after timeouts on grain and alternating. All nine point estimates happened to be within 10%, but the two uncalibrated results remain unsatisfied. Median wall was 2.979 s. Exact continuous jobs usually spent more than 100% of source duration because a padded measurement preceded the full export. This is not a low-budget sampling success.

Segmented mode completed only Megamind exactly, in 6.051 s. The other eight requests retained uncalibrated estimates after six probes; the conservative remaining-job estimate prevented admission of a complete export within the remaining budget. Eight of nine point estimates were within 10%, with the same 13.775% CBR-burst counterexample. There is no claim of eighteen full outputs or universal fallback completion. See `fallback/{continuous,segmented}/summary.json` and the per-request stop reasons/answers.

## Reproduce

```bash
.venv/bin/python benchmarks/core_validation.py engine --budgets 5 --sampling-plan content --sample-seconds 1 --segment-seconds 1 --output results/core-validation-v2-new
.venv/bin/python benchmarks/core_validation.py ab-av1 --sample-seconds 1 --output results/core-validation-v2-new
.venv/bin/python benchmarks/core_validation.py fallback --sampling-plan content --sample-seconds 1 --segment-seconds 1 --output results/core-validation-v2-new
.venv/bin/python benchmarks/core_validation.py quality --output results/core-validation-v2-new
```

Result directories cannot be overwritten. Source identities, full-reference SHA-256, resource costs, first-estimate times, feature-cache behavior, actual export semantics and uncertainty refusals are retained. No industrial calibration/holdout accuracy is part of this development report. Seven calibration source groups still cannot support a finite distribution-free 95% conformal envelope; sampled results therefore carry no invented interval. Continuous context bias, the remaining burst error, compression cost of short units, budget admission and independent industrial validation remain open requirements.
