# Continuous x264 probes: state diagnosis and repair experiments

Status: source audit and a small real API capability test, 2026-10-03. This does
not establish an accuracy improvement, confidence coverage, or industrial
acceptance. Production `src/` and the frozen native holdout were not changed or
used for tuning. The separate feed-future / stop-without-flush experiment is
owned by the streaming-context investigation and is not duplicated here.

## Verified implementation and API evidence

The installed encoder identifies itself as x264 core 164, r3108, `31e19f9`.
The Debian library package is `2:0.164.3108+git31e19f9-2+b1`. The inspected
upstream files are pinned to
[`mirror/x264` commit `31e19f9`](https://github.com/mirror/x264/tree/31e19f9),
downloaded into ignored `.research/x264-r3108/`. This matches the upstream
revision of the installed library; it does not assert that a new upstream build
has identical Debian build flags or patches. Download URLs and SHA-256 hashes
are recorded in
[`api-diagnostic.json`](../results/encoder-state-diagnostic-2026-10-03/api-diagnostic.json).

A deterministic one-second 64×48, eight-frame yuv420p fixture was actually
encoded with the installed FFmpeg/libx264, CRF 23, medium, one thread:

| Configuration | Result | VCL NAL count | VCL NAL bytes |
|---|---|---:|---:|
| Ordinary CRF | Success | 8 | 1,272 |
| CRF, `-pass 1 -fastfirstpass 0` | Success; identical VCL SHA-256 | 8 | 1,272 |
| CRF, `-pass 1`, default fast first pass | Success; different VCL SHA-256 | 8 | 1,232 |
| CRF, `-pass 2`, existing first-pass stats | Rejected: `CRF/CQP is incompatible with 2pass.` | 0 | 0 |

The compared hash covers the concatenated VCL NAL bytes, excluding start codes,
SPS/PPS and initialization SEI. It is
`46967ac898acf56f10795ede1a117e785747f509c56a3839c86e804a3ba5dde0`
for the first two rows. This tiny example establishes API behavior only.
It does not prove stats writing is bit-identical for every supported input.
Exact commands, source hash, return codes and timings are in the JSON record;
raw data and logs are in ignored `.research/encoder-state-diagnostic/`.

The practical diagnostic command is an ordinary target encode plus
`-pass 1 -fastfirstpass 0 -passlogfile UNIQUE_PATH`. The default
`fastfirstpass=true` changes reference count, motion search, subme, trellis and
other settings. It must be disabled when comparing original CRF semantics.
Use unique paths for each run and verify VCL equivalence on the actual
development clips before trusting this instrumentation.

## Which CRF state actually matters

[`ratecontrol.c:752–775`](https://github.com/mirror/x264/blob/31e19f9/encoder/ratecontrol.c#L752)
sets `b_abr` for both CRF and ABR when stats are not read. Consequently the
comment `1pass ABR` around the shared path is not evidence that CRF is
stateless. Conversely, merely seeing running bitrate variables in this path
is not evidence that they influence default CRF's P-frame quantizer.

With MB-tree enabled, the same initialization sets **internal**
`rc->qcompress=1`, while the user parameter `param.rc.f_qcompress` retains its
default 0.60. In
[`get_qscale()`](https://github.com/mirror/x264/blob/31e19f9/encoder/ratecontrol.c#L1998),
the MB-tree branch uses frame duration, rather than `blurred_complexity`.
For an ordinary nonzero-cost P frame in 8-bit CRF, no VBV, no zones or forced
QP, before clipping, this reduces to

```text
baseQP = CRF + (1 − user_qcomp) × 13.5
         + 6 × (1 − user_qcomp) × log2(0.04 / clipped_frame_duration)
```

[`ratecontrol.h:34–40`](https://github.com/mirror/x264/blob/31e19f9/encoder/ratecontrol.h#L34)
defines the duration center as 0.04 seconds and clips ordinary durations to
0.01–1.00 seconds. The artificial eight-fps fixture therefore predicts
`23 + 5.4 + 2.4 log2(0.32) = 24.45`; its real stats report `q:24.45` for
the P frame. The short-term SATD average is updated, but is not the ordinary
P-frame base-QP driver under these conditions. Zero-cost fallback, VBV,
different MB-tree settings, zones, forced quantizers, bit depth and duration
changes require separate treatment.

There are still important coupled states:

* **Macroblock quantizers.**
  [`macroblock_tree_finish()`](https://github.com/mirror/x264/blob/31e19f9/encoder/slicetype.c#L1029)
  computes an offset of the form
  `AQ_offset − 5(1 − user_qcomp) × [log2((intra + propagated)/intra) + weightdelta]`.
  Future references, lowres motion, weighted prediction and the chosen B/P
  structure change propagated importance. Higher importance lowers quantizers
  and can increase bytes even when the frame base QP is unchanged.
* **Reference pictures.** The full-resolution reconstructed DPB depends on
  earlier coding decisions. A new short encoder starts with a different IDR
  and reconstructed chain. Equal raw source pixels at the sampled PTS do not
  imply equal inter-prediction residuals, reference selection or coded bytes.
* **Frame-type and boundary state.** Lookahead retains the last non-B frame,
  last keyframe position, source timestamps and buffered future frames.
  [`slicetype_analyse()`](https://github.com/mirror/x264/blob/31e19f9/encoder/slicetype.c#L1473)
  limits search for deterministic operation, with explicit end-of-stream
  behavior. Ending a probe is a different event from continuing the source.
* **I/B frame base QP.** B-frame base QP uses adjacent reconstructed reference
  frames' `f_qp_avg_rc`, not an independent SATD-to-QP rule. A later I frame
  can use the exponentially weighted `accum_p_qp / accum_p_norm` divided by
  the I/P factor. That accumulator is updated in coded order, including B
  frames despite its name. It decays by 0.95 rather than resetting at an IDR.
  [`reference_reset()`](https://github.com/mirror/x264/blob/31e19f9/encoder/encoder.c#L2563)
  clears reference pictures; it is not a complete rate-control reset.

Thus a P-byte ratio near 0.4–0.5 should first be separated into base QP,
macroblock offsets, frame/reference structure and reconstructed reference
content. “CRF's bitrate history” is not yet a demonstrated cause.

The ordinary debug frame log prints average QP **after** AQ/MB-tree.
First-pass stats distinguish `q:` (before AQ) and `aq:` (after AQ), plus
`tex:`, `mv:`, `misc:`, `ref:` and the display/coded `in:`/`out:` mapping.
[`ratecontrol_end()`](https://github.com/mirror/x264/blob/31e19f9/encoder/ratecontrol.c#L1829)
also writes `.mbtree` offsets for kept reference frames. These make a useful
diagnostic surface without claiming that the file can restore CRF state.

## Smallest informative development experiment

Freeze source hashes, target configuration, integer source frame range and
diagnostic settings before running. Start with the already frozen
development `pedestrians` and `alternating` cases; add one known high-motion
failure only if it was designated development before measuring it. Do not
select native holdout clips according to their observed errors.

1. For one complete encode and one existing padded probe per source, add stats
   writing with fast first pass disabled. Verify the original and instrumented
   VCL packets match before interpreting stats. Join records using display
   frame index plus the probe's verified source offset, and use packet PTS for
   the half-open central interval. Keep coded order for state recurrences.
2. For each common central PTS, record picture type, reference flag, base `q`,
   `aq`, VCL bytes, texture/motion bits and reference counts. Report matched-P
   pairs separately from frame-type mismatches. A P-only aggregate conceals
   changes in which source frames became P frames.
3. Compare `.mbtree` records for matched reference frames after validating
   type/order and dimensions. Export mean and quantiles of offsets, not only
   an average frame QP. Use the pinned implementation's packing/unpacking
   functions rather than guessing byte order or scaling. Offsets are stored
   for kept reference frames; there is no record for every displayed frame.
4. If base QP agrees but offsets differ, run **one** controlled paired ablation:
   MB-tree disabled in both the full reference and its probe, with all other
   declared settings held fixed. If needed, separately set AQ strength to
   zero. These are mechanism tests with changed targets, not fixes to the
   frozen original export. `aq-mode=0` alone does not disable MB-tree: x264
   changes it to AQ mode 1 with strength zero when MB-tree remains enabled.
5. If structure/offsets agree but VCL bytes differ, instrument the pinned
   research build to hash the reconstructed reference luma/chroma planes and
   log reference POCs. Instrument just before reference-list construction and
   rate control; validate the research build against the installed encoder
   first. This directly tests reference-chain mismatch rather than adding an
   unexplained global correction factor.

The sibling same-library no-flush experiment answers whether premature EOF
is sufficient to explain the difference. Its negative result would not show
that reference or MB-tree history is irrelevant. The measurements above
separate those residual mechanisms without repeating the sibling helper.

## Repair options and their honest cost

**Keep a live encoder for one fixed CRF.** Feeding the exact original prefix,
with the same pixel pipeline, timestamps and future buffering, can preserve
the actual state. NALs already produced can be cached and reused in that same
continuous export. The public API has no snapshot/restore or reconstructed
DPB-import function. `x264_encoder_parameters()` copies configuration;
`x264_encoder_reconfig()` changes selected parameters. Neither copies state.
Seeking forward without reconstructing intervening references cannot be an
exact continuation.

This is implementable for a prefix-then-continue job and for repeated jobs
with a previously built prefix cache, but reaching a late sample requires
encoding the prefix. Its cost must be charged. Switching the live encoder
from CRF A to CRF B does not create the result of a fixed-B encode from the
beginning: the reference pictures were reconstructed under A. A reusable
multi-CRF design needs separate live states or explicitly changed output
semantics. A single-thread quiescent process checkpoint could be a research
way to clone existing state, but does not save the initial prefix work and
is not a portable x264 state API.

**Stats read / two pass.** Original CRF rejects stats read before opening the
stats file (`ratecontrol.c:775`). ABR two pass, even with excellent predicted
size, changes the target export contract. CRF stats write is a diagnostic
tool, not a cheap first pass and not a CRF checkpoint. `.mbtree` records alone
omit reconstructed references, frame counters, lookahead buffers and other
state. Public `picture.prop.quant_offsets` adds offsets on top of x264's own
decisions; injecting saved MB-tree offsets into an MB-tree-enabled encoder
can apply the effect twice. Any external-plan import requires an explicit
verified research modification, not toggling `stat_read` while claiming
ordinary CRF equivalence.

**Encoder-native global analysis, then natural-GOP sampling.** The promising
way to remove arbitrary-probe boundaries is to use the original encoder's
full source-order lookahead plan: source frame types, natural IDR positions,
AQ/MB-tree maps and frame-base QP state. Start selected coding runs at those
actual closed-GOP IDRs and preserve planned future context. The cheap pass
would skip full-resolution reconstruction, motion RDO and entropy coding;
it must still perform the original lowres motion/SATD/MB-tree analysis for
every input frame. This is not available as a stand-alone public x264 API.

A minimal pinned-source research fork can first export the lookahead plan
without modifying any final encoding decisions; a second step can stop after
analysis for unselected GOPs and import the frozen plan into sampled GOPs.
The first acceptance test is exact per-frame VCL byte-count agreement against
the unmodified full encode on development sources, followed by a separately
frozen test. Equal average error is not enough to establish state equivalence.
An IDR clears the DPB but does not alone restore base-QP history, counters,
header choices or lookahead. All such metadata must either be reproduced or
shown irrelevant by exact comparison. Begin with deterministic one-thread,
8-bit SDR, explicit CFR, closed GOP, no VBV, no zones, no intra refresh; broader
settings need separate implementations and families.

Natural GOPs can be long: the default 250-frame keyint is about 10.4 seconds
at 24 fps. Sampling a complete GOP plus future context may exceed a requested
small encoding fraction. Refuse or offer an explicit higher budget in that
case. This is still a continuous-target method: it must reproduce the
original natural GOPs, not introduce new independent segments and compare
their size as though the target were unchanged.

The native lowres image is half the encoder input width and height
([`frame.c:123`](https://github.com/mirror/x264/blob/31e19f9/common/frame.c#L123)),
and exists for every input frame. At native 4K, that is approximately a
1920×1080 luma analysis surface with motion searches, not the current small
four-fps preview. Full-source decode, pixel transforms, analysis time and
memory must all be measured and charged to wall budget. Reusing a cached
analysis plan can improve repeated requests; its ingestion cost belongs in
cold end-to-end results. Cache identity includes source content, transformed
frame timeline, pixel format, all analysis settings, implementation revision
and threading semantics.

**SATD proxy as a predictor.** Source-wide SATD, intra/inter ratio, propagated
importance, reference structure and scene changes can predict the probe-to-
continuous residual more directly than an arbitrary motion score. They do
not restore the reconstructed DPB or prove a byte ratio. The restricted
MB-tree CRF base-QP formula above also explains why using SATD solely to
“correct the frame QP” can target the wrong mechanism. A learned or fitted
residual model is a separate versioned empirical predictor, with actual
analysis cost and source-group validation. Its confidence claims require
calibrating the complete prediction trajectory, including this residual;
the proxy is not a distribution-free guarantee by itself.

## What sampling standard error cannot certify

Let `y_i` be bytes in source interval `i` of the actual complete continuous
encode and `x_i` be the corresponding warmed-probe measurement. Sampling
estimates the sum of `x_i`. The target difference includes both

```text
sampling error around sum(x_i) + sum(x_i − y_i).
```

More samples reduce the first term; an encoder-state bias remains in the
second. Even a census of every interval does not remove it. If every probe
has `x_i=c` and every target interval has `y_i=2c`, the observed variance and
sampling standard error are zero while the relative bias is −50%. This is a
statistical counterexample, not an assertion that x264 always has this ratio.
The actual development census already demonstrates nonzero context bias;
[`context-study-2026-10-03`](../results/context-study-2026-10-03/README.md)
documents remaining GOP/frame-type differences after finite padding.

No universal finite padding or probe-budget accuracy bound follows from the
public API or these measurements. A hidden unobserved high-cost interval can
also defeat a low-budget black-box estimator without a justified range or
distribution assumption. This motivates explicit alternatives: exact
independent-segment accounting under its own export semantics, a verified
encoder-native continuous analysis contract, or an honest uncalibrated
continuous predictor until sufficient independent source groups exist.
Trajectory conformal coverage remains marginal and assumption-dependent;
it must include state/model error, stay inside the frozen family, and does
not become a conditional or per-video guarantee from a low sampling SE.
