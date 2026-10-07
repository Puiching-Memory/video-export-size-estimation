# Core development validation: policy v1

The reusable segmented export fixes the probe-versus-final-file context mismatch: every sampled segment was reused unchanged, all nine assembled files decoded, and their actual sizes matched the additive accounting exactly. This does **not** solve reliable estimation at a small sampling budget. Fixed temporal pilots missed short complexity bursts, causing errors approaching 98%. No sampled prediction was declared certified or satisfied, and these results do not establish SOTA or industrial acceptance.

## Scope and reproducibility

Only the nine previously opened development sources were used: seven synthetic 24-second clips, the 40-second pedestrians excerpt, and the 10.93-second Megamind excerpt. No industrial `calibration_reserved` or `test_reserved` accuracy was read or used for tuning. All exports use libx264, CRF 23, medium, yuv420p, one encoder/filter thread, no scaling or frame-rate conversion, and no audio. The development sources contain no audio; this benchmark supplies no AAC acceptance evidence.

Continuous estimates are scored against fresh complete ordinary MP4 exports with identical settings. Segmented estimates are scored against their own complete independent closed-GOP four-second fragmented MP4 exports. The two targets are never mixed. Predictions were persisted before complete labels were generated or read within each stage. The independent segmented measurements were subsequently used as labels for the controller measurements; they were not inputs to the estimator.

The four-CPU cloud cgroup was shared with other agents' small regression jobs. Job order was fixed, and timings are diagnostics rather than a statistically controlled speed comparison. `cold-original` means a fresh application cache and real input hashing; the OS page cache was not flushed. `prepared` uses a Linux sealed memfd snapshot, with one-time copy/hash cost reported separately and also added to the request time in an end-to-end timing field. It does not hide ingestion cost. Preparation took 0.25–22.0 ms on these small sources; this is not a large-file ingestion benchmark.

Parallel robustness and formatting changes were made between stages. Each stage records its source-code SHA-256, but the complete experiment is **not** claimed to run on one immutable commit. The prediction policy in the controller stage used three fixed temporal pilots followed by randomized residual audits, the original `2048 + 6/frame` ordinary-MP4 model, and context padding with optional periodic-I correction. Each controller answer includes its actual prediction-family fingerprint. The post-run diagnostic source snapshot is `artifacts/core-validation/v1-source.zip`, SHA-256 `bbc1216247d0e9b08d35ff44f60974380fd40a6268d9bccb81f2bfa918f6ea42`; its later source fingerprint is `51248e6bb417a84121c45498fd4935ad91ce3f764468e617a288e4cfc4539d11`. It does not pretend to be the exact source snapshot of every earlier stage. Future runs save source snapshots at stage start; v2 must use a fresh process and a separate output directory after code is frozen.

## Controller results under a real budget

There were 72 requests: nine sources × two explicit export modes × two encoded-video fractions × original/prepared input. Each had a five-second wall budget and at most six new probes. Padding was charged in full, rather than only its central measurement window. No request exceeded its encoded-video fraction. Wall cancellation had a measured worst overrun of 73 ms. All sampled predictions remained uncalibrated; none was falsely marked `satisfied`.

| Mode, cold original input | Video budget | Estimates | Within 10%, all nine cases | Median APE among estimates | Worst APE | Median actual charged fraction | Median total wall | Median first estimate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Continuous | 20% | 9/9 | 3/9 | 31.406% | 98.313% | 16.67% | 0.798 s | 0.491 s |
| Continuous | 50% | 9/9 | 5/9 | 7.741% | 97.552% | 48.96% | 1.827 s | 0.985 s |
| Segmented | 20% | 8/9 | 5/9 | 2.066% | 98.268% | 16.67% | 0.735 s | 0.677 s |
| Segmented | 50% | 9/9 | 6/9 | 0.686% | 98.268% | 50.00% | 1.534 s | 0.739 s |

The missing segmented result is Megamind: its first four-second export segment cannot fit a 20% video budget. It is counted as a failure in the all-case success denominator. Prepared-input accuracy was identical except for deadline-driven available work; small-file timing improvements were inconsistent. See `engine/continuous/summary.json`, `engine/segmented/summary.json`, per-request answers and update streams.

The independent component study separates sample-selection error from context error. With three probes, context plus periodic correction had median file APE 3.302%, but only five of nine cases were within 10%; median actual encoding fraction was 73.44%. Six padded probes cost a median 146.88% of source duration. These are not low-budget wins. Prefix-six component summaries contain eight sources because Megamind has only five two-second blocks; prefix-six independent-segment summaries also contain eight sources because Megamind has only three four-second segments. All counts are explicit in the JSON.

The static continuous reference had 18,261 file bytes, 10,490 payload bytes and 7,771 container bytes. Its `ctts` table alone had 573 runs occupying 4,600 bytes; `stsz` occupied 2,324 bytes. The ordinary `6/frame` overhead heuristic was too small. A probe-table extrapolation gave 7,699 container bytes, only 72 bytes below the reference, but this is a post-hoc development diagnosis, not part of the frozen v1 controller. The complete box decomposition is in `components/static-container-boxes.json`.

## Competitive baseline

ab-av1 v0.11.7 was run on all nine sources using three two-second samples, libx264 medium CRF 23, explicit keyint 250, one encoder/input/filter thread, no VMAF computation, no estimator cache, and the same five-second deadline. All nine produced estimates. Its unmodified official size prediction had median file APE **3.295%**, six of nine cases within 10%, and worst APE **73.340%** on CBR burst. Median wall time was 1.203 s and median actually encoded fraction was 25%. This fraction was 15% for pedestrians and 54.96% for Megamind, so it is not a common 20% or 50% admission test.

The log describes the prediction as “video stream size,” but preserved input MKV/output MP4 samples show container bytes affect the bitrate ratio. Main comparison uses the raw official prediction against complete MP4 file size. Payload error is also recorded, with no ad hoc addition or subtraction of overhead to improve its score. For static, `18,154 × 2,984 / 1,885 ≈ 28,738` reproduces the reported prediction from source/input-sample/output-sample file sizes. See `ab-av1/evaluated.json` and the exact commands and preserved sample metadata in `ab-av1/predictions.jsonl`.

This baseline performs better than v1 on several cases and remains a relevant competitor. The nine tiny, already-opened development cases do not support a superiority claim for either method.

## Segmentation costs and exact accounting

All nine completed segmented files passed full FFmpeg decoding. The writer's known initialization/fragment overhead plus actual packet payload equaled file size exactly. Every previously sampled segment was reused from cache. At a complete census, seven synthetic cases at six segments and Megamind at three segments had exactly zero-byte prediction error. Those censuses consumed 100% of video duration and are evidence of structural consistency, not cheap prediction. Pedestrians at six of ten segments retained 2.497% error.

| Case | Segmented file-size change versus continuous | PSNR change | SSIM change |
|---|---:|---:|---:|
| static | +34.659% | Both infinite | 0 |
| motion | +1.408% | −0.054 dB | −0.000031 |
| grain | +0.019% | +0.004 dB | +0.000563 |
| brief-burst | −0.173% | −0.058 dB | −0.000081 |
| cbr-burst | +1.245% | −0.110 dB | −0.000180 |
| alternating | −3.319% | −0.367 dB | −0.002054 |
| screen | +10.008% | +0.064 dB | +0.000025 |
| pedestrians | +30.631% | −0.382 dB | −0.001050 |
| megamind | +2.479% | −0.124 dB | −0.000296 |

Both PSNR and SSIM compare each final export to the original decoded development source at matching presentation timestamps. These results establish a material compression/quality tradeoff for explicit segmentation, especially on static and low-FPS content. They do not establish perceptual equivalence. Exact metric commands and values are in `segmented/quality.json`.

## Actual hard-cap search and memory

Six additional requests used a cap equal to 70% of each mode's CRF-23 reference, a 15-second wall budget, at most five CRF candidates and no probes. This is a completed-export/quality-search test, not a low-cost-estimation test. Every satisfied artifact was materialized, rechecked for size and SHA-256, and fully decoded. That external verification time is reported separately from the controller budget.

| Case/mode | Outcome | Selected CRF | File/cap bytes | Quality evidence |
|---|---|---:|---:|---|
| motion continuous | Satisfied | 27 | 578,765 / 597,276 | All lower allowed integer CRFs measured and oversized |
| motion segmented | Satisfied after refinement cancellation | 27 | 588,774 / 605,683 | Best verified; CRFs 25/26 remain unverified |
| grain continuous | Budget exhausted; no publication | 23 | 5,987,083 / 4,190,958 | No verified feasible setting |
| grain segmented | Budget exhausted; no publication | 23 | 5,988,249 / 4,191,774 | No verified feasible setting |
| pedestrians continuous | Satisfied | 26 | 395,181 / 411,556 | All lower allowed integer CRFs measured and oversized |
| pedestrians segmented | Budget exhausted; no publication | 24 | 688,664 / 537,620 | No verified feasible setting |

The motion segmented run demonstrates the important cancellation behavior: an already verified feasible CRF-27 file survives a timed-out attempt at higher quality. The result reports `best_verified` rather than asserting global optimality. Three failed searches honestly refused publication. All raw observations, budgets and search summaries remain in `hardcap/`.

The first memory monitor depended on `/proc/<pid>/task/<pid>/children`, which this managed environment omits; its measurements are marked unavailable rather than reporting a false zero. A corrected PPid/stat monitor then measured six separate full-export requests for motion/grain/pedestrians. Fifty-millisecond samples showed approximately 94–105 MB peak process-tree RSS. Sealed snapshot pages not mapped into a process are outside RSS, so snapshot storage bytes are reported separately, up to 16.08 MB here. Host/cgroup total memory was not measured. See `memory/results.json`; these small-file measurements do not validate native-4K or concurrent production capacity.

## Reproduce with a new output directory

```bash
.venv/bin/python benchmarks/core_validation.py components --output results/core-validation-new
.venv/bin/python benchmarks/core_validation.py segmented --output results/core-validation-new
.venv/bin/python benchmarks/core_validation.py engine --budgets 5 --output results/core-validation-new
.venv/bin/python benchmarks/core_validation.py ab-av1 --output results/core-validation-new
.venv/bin/python benchmarks/core_validation.py hardcap --cases motion,grain,pedestrians --output results/core-validation-new
.venv/bin/python benchmarks/core_validation.py memory --cases motion,grain,pedestrians --output results/core-validation-new
```

Stages refuse to overwrite existing result directories. The runner verifies the frozen development manifest and input SHA-256, rejects industrial accuracy sources, snapshots code at stage start, persists predictions before labels, and records complete references and actual encoded costs. Binary exports, estimator caches and code snapshots are under ignored `.vsize-cache/` or `artifacts/` directories. To reproduce policy v1 after v2 changes defaults, use the diagnostic source archive and account for its documented post-run robustness-fix difference; to evaluate v2, freeze its implementation and run the current script into a separate new directory.

Unresolved core work is explicit: discover bursts without spending a full expensive decode, fit a reliable ordinary-MP4 overhead model, improve latency/work admission on short clips, establish independent calibration support and evaluate a frozen policy on industrial held-out sources. Seven existing calibration groups cannot support a finite distribution-free 95% conformal envelope. Neither low-budget reliability nor industrial SOTA has been established by this experiment.
