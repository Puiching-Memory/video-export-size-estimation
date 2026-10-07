# Independent-probe startup candidate, original development set only

The packet correction removes much of the one-second restart inflation, but it
does **not** solve continuous-encoder prediction. At a 25% encoded-video budget,
the median absolute file error falls from 14.15% to 4.85%; pedestrians changes
from +135.85% to **−33.71%**. Eight of the nine familiar development cases finish
within 10%, while the remaining case is a material failure. This is a development
ablation, with no SOTA, generalization, calibrated interval, or industrial-success
claim. Frozen v2 runtime code and holdout accuracy were not used or changed.

## Fixed policy and real packet measurements

`metadata.json` fixes the formulas before this script measures or scores cases.
Its policy fingerprint is
`3c31f341f6702fbce9a3aab114671261f3823ef0b708c2b35496f5d7582fcf7c`.
Selection uses v2's 4 fps, 96×54 mean texture/change preview, source metadata
blocks of one second, at most three content representatives, seeded random
audits, and at most six probes. Nominal pilot count is limited by the encoded
fraction; the flat static case has one distinct content representative, and
megamind has two pilots and two total probes under both budgets. Selection and
prediction use neither reference bytes nor case-specific fitted parameters.
This is a familiar development set whose earlier failure labels motivated the
candidate, **not a blinded evaluation**. Label separation means that the
prediction functions do not consume reference values during this run.

Prediction rows explicitly have `phase=prediction` and
`prediction_uses_reference=false`. `evaluated.json` and final-prefix rows have
`phase=assessment`; complete-reference and matched-window records have
`phase=assessment_diagnostic` and `scoring_only=true`. `phase-log.json` documents
the executable phase order. The original run did not capture UTC phase-event
timestamps; its log says so explicitly. Phase annotations were added at archival
review without changing any of the 258 prediction values or errors. Future
script invocations record actual UTC events and the saved prediction-file hash
before opening development references.

All video exports are libx264, CRF 23, medium, yuv420p, passthrough frame timing,
video only, MP4 faststart, one codec/filter thread. Full reference encodes use the
same effective encoder settings. The configuration's `segment_seconds` differs
between continuous references and probes, but is not an FFmpeg argument for a
continuous export. Parsed x264 user metadata independently confirms `threads=1`
and `keyint=250` in every measured probe. Actual fresh commands, metadata reads,
preview commands and packet inspection commands are in `commands/`; cached
probes retain their original configuration, source/toolchain hashes, artifact
hash, encoding time and complete x264 options in `measurements/`.

AVCC length fields come from `avcC` extradata. A NAL type 6 is subtracted only
when every parsed SEI message inside it is type 5, user_data_unregistered. This
counts serialized NAL bytes and AVCC framing, rather than just its message body.
Every one of the 50 measured probes contains **690 B** of such startup SEI:
680 B message, six bytes of NAL/message/trailing framing, and a four-byte AVCC
length. All other SEI and all subsequent keyframe packets are retained.

For a probe, define `E = max(0, first_keyframe_bytes − startup_SEI − mean_nonkey_bytes)`.
The nonkey mean includes exactly packets whose MP4 keyframe flag is false; it
does not assume every non-I picture has the same size. The three payload models
are:

1. Extrapolate raw probe payload using the unchanged content/audit Sampler.
2. Extrapolate raw payload minus measured startup SEI, then add the maximum
   observed SEI once globally.
3. Extrapolate raw payload minus measured startup SEI and E, then add global SEI
   once and `max(1, ceil(metadata_frames / 250) − estimated_nonstartup_keyframes)
   × mean(observed_E)`.

The final restoration always includes the initial global keyframe. Extra
observed keyframes remain in the corrected base and are extrapolated by the
same Sampler, so they do not receive another blind periodic restoration. None
of these 50 short probes happened to observe a nonstartup keyframe; therefore
this protection is not an empirically exercised strength of this development
run. True continuous megamind has four keyframes, but its selected short probes
have only their startup keyframe. Scene-cut discovery remains a separate error.

All three models use the same `mux_model` census-based container prediction,
with expected keyframe count at least both the periodic count and one plus the
extrapolated nonstartup count. The original v2 default container estimate is
recorded separately to make this baseline difference visible. MP4 ctts run
density is still extrapolated from independently drained short GOPs, so it is
also an empirical point model.

The requested initial examples are real measurements, before any scoring:

| Probe | Raw payload | First I after SEI | Nonkey mean | First-I excess E |
|---|---:|---:|---:|---:|
| static, 18–19 s | 1,139 B | 71 B | 16.43 B | 54.57 B |
| screen, 14–15 s | 7,513 B | 3,308 B | 152.83 B | 3,155.17 B |
| pedestrians, 34–35 s | 31,173 B | 26,506 B | 441.89 B | 26,064.11 B |

## Complete file results, all nine cases

Signed errors below use the final allowed prefix and the real complete
continuous MP4 file size. Methods share the same selected packets and container
prediction. Additional early prefixes and separate payload errors are preserved
in `evaluated.json`, without selecting the best prefix after seeing the label.

| Case | 20% probes | Raw | SEI only | SEI + first I + periodic | 25% probes | Raw | SEI only | SEI + first I + periodic |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| static | 4 | +91.33% | +4.42% | −1.85% | 6 | +91.33% | +4.42% | −1.85% |
| motion | 4 | +7.75% | +5.89% | −4.65% | 6 | +9.84% | +7.98% | −2.30% |
| grain | 4 | −1.23% | −1.50% | −3.52% | 6 | +1.05% | +0.78% | −1.84% |
| brief-burst | 4 | −1.19% | −2.30% | −2.39% | 6 | −1.15% | −2.27% | −2.35% |
| cbr-burst | 4 | +14.14% | +11.71% | +8.79% | 6 | +14.15% | +11.72% | +8.25% |
| alternating | 4 | −3.19% | −3.57% | −5.57% | 6 | −2.89% | −3.27% | −5.21% |
| screen | 4 | +59.97% | +46.44% | −9.17% | 6 | +61.28% | +47.75% | −8.87% |
| pedestrians | 6 | +135.85% | +131.28% | **−33.71%** | 6 | +135.85% | +131.28% | **−33.71%** |
| megamind | 2 | +16.94% | +14.32% | −4.85% | 2 | +16.94% | +14.32% | −4.85% |

At 20% budget the corresponding median absolute file errors are
14.14%, 5.89%, and 4.85%; maximum errors are 135.85%, 131.28%, and 33.71%.
At 25% budget the medians are 14.15%, 7.98%, and 4.85%, with the same maxima.
The correction worsens already reasonable grain, brief-burst, and alternating
predictions. Those counterexamples are retained, not removed or specially gated.

## Pedestrians remains a real counterexample

Reference packets are parsed only in the scoring/diagnostic phase, after all
candidate predictions are persisted. A separate `matched-window-diagnostics.json`
compares probe packets to **the exact same source time window** in the continuous
reference. This avoids attributing a content-selection difference to encoder
state. It is an explanatory diagnostic, never a prediction input.

| Pedestrians window | Probe P mean | Continuous P mean in same window | Probe/continuous P size | Probe nonkey mean | Continuous nonkey mean |
|---|---:|---:|---:|---:|---:|
| 2–3 s | 1,208.00 B | 2,698.00 B | 44.8% | 832.44 B | 1,378.80 B |
| 11–12 s | 1,558.33 B | 3,062.33 B | 50.9% | 1,032.22 B | 1,409.30 B |
| 12–13 s | 1,580.00 B | 3,236.50 B | 48.8% | 1,047.78 B | 1,226.10 B |
| 22–23 s | 1,198.00 B | 2,901.00 B | 41.3% | 800.44 B | 1,260.20 B |
| 27–28 s | 1,316.00 B | 2,705.67 B | 48.6% | 913.44 B | 1,292.10 B |
| 34–35 s | 656.50 B | 1,638.25 B | 40.1% | 441.89 B | 829.70 B |

The slice-header parser identifies P/B/I without a pixel decode. Packet-level
picture types were cross-checked against real FFprobe frame decoding on static,
screen, and pedestrians; every frame agreed. Parser verification also covers
extended SEI lengths, other SEI message types, emulation-prevention bytes and
truncated slice headers (`parser-verification.json`).

These measurements establish that the discrepancy is not confined to the
startup I packet: matched P packets are much smaller in every independent
pedestrians probe. The selected short probes also have different P/B counts.
The continuous file's 137 P pictures average 2,559.58 B, compared with only
656.5–1,580 B in selected probes. There may be ratecontrol, mbtree/lookahead,
reference-chain and truncated-tail causes; these data do **not** identify a
particular QP mechanism. A first-I subtraction alone cannot recover this missing
nonkey payload. Multiplying by a fitted pedestrians-specific constant would
not be a demonstrated general solution.

Screen's matched P packets also fall to 80.2–87.2% of continuous P size, explaining
why substantial residual underestimation remains after removing its I inflation.
Measuring normal-picture state, or obtaining a genuinely representative longer
context, remains necessary before claiming continuous prediction is fixed.

## Next mechanism experiment, not another accuracy sweep

The next useful development experiment should hold the central source window
fixed and vary **leading context × trailing context** in a 2×2 design. Pick
windows from the already-frozen cheap-preview content strata before measuring,
including quiet, changing, and burst-boundary windows in the old development
cases. Compare no prefix versus an explicit two-second prefix, and no suffix
versus a suffix covering the preset's lookahead plus B delay. Keep the full
central PTS window and account the entire actually encoded padded span.

First verify that the selected input pixels agree with the continuous source
window, to remove seeking/frame-phase differences. Then compare the same
central nonkey P/B packet sizes, frame types and reference-B flags against the
full continuous reference; include x264 per-picture QP only if it can be
observed reliably in the actual encoder trace. A suffix-only improvement would
identify a truncated-future contribution; a prefix-only improvement would
identify a startup-history contribution. An interaction or persistent residual
would demonstrate that neither one-dimensional fix is sufficient.

This is a diagnostic research budget, not a new promise that padding fits the
existing controller fraction. Do not fit a pedestrians-specific multiplier or
choose the best padding after looking at already-scored holdout results. The
primary question is whether independent short exports can identify ordinary
continuous-picture cost at all under the constrained budget. If the mismatch
survives the state ablations, the industrial API must keep continuous state
approximation explicit while offering separately priced exact completion or
the independent-segment contract.

## Cost and reproducibility

50 real one-second artifacts are inspected: 48 verified v2 cache artifacts and
two newly encoded grain windows (3–4 s and 20–21 s). Fresh preview preparation
performs one whole-source 4 fps decode per development source; it is explicitly
charged and its full source duration is recorded. Sources use only stream/format
metadata to construct their plan, with no mandatory full source packet scan.

Every replayed policy is charged source hash/metadata setup, the recorded cold
whole-source preview time, the original artifact's cold encoding time, current
cache validation/AVCC+census inspection time, and candidate inference CPU. Probe
payload can be reused for these ablations, but it is charged independently to
each policy. Benchmark JSON/log writes and final source verification are
excluded; no hard deadline-success claim follows from the replayed time field.
Reference packet inspection and parser cross-checks are scoring-only costs,
outside candidate inference. Complete probe PTS/DTS and both requested/actual
encoded duration are saved.

At 20% budget, median cost is 2.365 s and median cost/full-encode ratio is 1.400.
At 25%, these are 2.620 s and 1.596. Eight cases have a replayed cost ≤5 s;
grain exceeds it (5.238 s / 6.730 s). These values mix historical cold encoding
times with current read/preview times and are **not** fresh deadline trials.
On these short assets, many complete encodes are still faster than discovery
plus repeated measurement. Adding more short probes cannot be called a free fix.

Reproduce from the repository root:

```bash
PYTHONPATH=src .venv/bin/python benchmarks/startup_study.py
```

The script writes its fixed policy first, computes and saves all predictions,
then opens development reference artifacts for scoring. It has no holdout input
option. `--cases` accepts only IDs in the old development manifest. Results and
cached artifacts can be replayed; delete this study's `.vsize-cache` to measure
cold preview preparation again. Existing v2 probes are always validated by
record checksum, source/toolchain/configuration key and complete artifact hash.

To collect **new probe artifacts**, use an empty output directory and disable
v2 reuse:

```bash
PYTHONPATH=src .venv/bin/python benchmarks/startup_study.py \
  --no-v2-reuse --output results/startup-study-fresh-artifacts
```

That performs fresh preview/probe artifact collection, then evaluates the same
shared-measurement replay policies. It still does **not** become a fresh
controller deadline experiment: each budget/method is charged replayed measured
cost rather than run independently under a real five-second deadline. No such
fresh controller trial is reported here.

Archival review found two generated grain MP4 files, about 520 KiB total, inside
the Git-ignored `.vsize-cache/`. No MP4, raw video, or other binary artifact is
eligible for normal `git add` of this result directory. The saved reference
diagnostics contain packet metadata and lengths, not encoded payload blobs.
`archive-check.json` records the reviewable text inventory and consistency checks.

Machine-readable files: `summary.json`, `comparison.csv`, `final-prefixes.json`,
`predictions.json`, `evaluated.json`, `features/`, `measurements/`,
`reference-diagnostics.json`, `matched-window-diagnostics.json`, `commands/`,
and `parser-verification.json`.
