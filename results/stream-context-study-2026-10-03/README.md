# Streaming libx264 context: exact central output without flushing future frames

The tested mechanism works: feed source frames to the same libx264 used by FFmpeg, wait until every central presentation timestamp has actually produced a packet, then close the encoder without flushing its queued future frames. Across **31 tested windows**, the central AVCC packets and decoded pixels exactly match the complete padded encode. This removes some future-frame encoding CPU while preserving the tested padded-probe result. It does **not** resolve all bias against a continuous full-file encode, and it does **not** establish the old request-fraction budget for a new backend.

The primary evidence is [summary.json](summary.json), [decoded-central-proof.json](decoded-central-proof.json), and the batch measurements linked below. The helper and reproducible driver are in [benchmarks/stream_context_study](../../benchmarks/stream_context_study/README.md).

## Configuration and evidence boundaries

- Old nine-source development manifest only; fixed requested leading context of 0, 2 and 6 seconds, one central second at the same source frame range for each source, 45 planned future frames. Megamind is short, so its requested 6 seconds is clamped to its available preceding frames.
- Previously inspected 60-second, 640×360 Bunny is a separate three-window mechanism check. An additional one-second-leading-context pedestrians window tests a budget hypothesis. No reserved corpus, holdout accuracy or model tuning is involved.
- Video only, libx264 medium, CRF23, yuv420p, one encoding/decoding/filter thread, lookahead40, B-frames3, default keyint250, AVCC, no repeated headers, VFR input flag enabled on the CFR frame clock. Audio, real VFR, arbitrary filter graphs, other presets and parallel encoding are outside this experiment's proof.
- FFmpeg 7.1.5 and the installed `libx264.so.164`, exact build `r3108 31e19f9`. Matching Debian development headers were extracted without system installation. Library/header hashes and build commands are saved in each batch metadata.
- Frozen v2 source `d300d1f`, fingerprint prefix `f39`, was copied to `artifacts/stream-context-f39/src` before v2.1 maintenance changes. All 16 snapshot file hashes match the study's starting source. Later batches import and hash this snapshot, not the changing working source.
- Cloud has a four-CPU quota and concurrent work. Wall ratios are single instrumented measurements, not confidence intervals or strict latency claims. Debug logging and `benchmark_all` add overhead.

Before accepting each window, the study required:

1. Raw source frame MD5 equality against the same decoded frame range from continuous source decoding; this excludes seek/pixel mismatch as a hidden warmup effect.
2. API full encoding versus FFmpeg encoding of the identical raw sample: each complete AVCC packet, packet count, PTS/DTS, SPS/PPS, and every decoded frame MD5.
3. API full versus **native-source** padded `Media.encode(window)`: each AVCC packet, count, PTS/DTS and SPS/PPS. This also checks that the raw bridge preserves the source encoding contract.
4. API stop versus API full: every central AVCC packet, PTS/DTS and SPS/PPS; the independent decode check also verifies every central frame MD5. All central presentation timestamps must be present exactly once.
5. Direct FFmpeg-to-helper full and stop pipelines versus their raw-file equivalents: each packet, PTS/DTS and SPS/PPS. There were 62 successful pipeline comparisons for the 31 primary windows.

**All these gates passed for all 31 primary windows.** Independent stop decoding produced one picture per emitted packet, including any future reference pictures; presentation order was mapped using the helper's recorded PTS, then only the central pictures were compared. The final current-driver smoke run additionally exercised the integrated decoded-central gate.

## Actual work: representative pedestrians window

Source contains 400 frames at 10fps. Central source frames are 120–129, with 20 preceding frames and 45 planned future frames. All values below are from the same direct-pipeline pair in `development-fixed/measurements.json`.

| Work or cost | Full padded encoding | Stop after all central packets |
|---|---:|---:|
| Source packets parsed | 196 | 196 |
| Source pictures decoded, including GOP preroll/read ahead | 185 | 185 |
| Raw pictures emitted by FFmpeg | 75 | 75 |
| Pictures fed to x264, including lookahead analysis | 75 | 72 |
| Packets actually emitted by x264 | 75 | 31 |
| Central packets | 10 | 10 |
| Future packets emitted | 45 | 1 |
| Queued pictures discarded by close | 0 | 41 |
| Calls with NULL input to flush | 41 | 0 |
| Decoder CPU, `wait4` user + system | 0.2153s | 0.2206s |
| x264 encode calls + close CPU | 0.2898s | 0.1482s |
| Helper total process CPU, including setup/input/output | 0.3016s | 0.1577s |
| Pipeline observed wall | 0.4444s | 0.3176s |

The one future emitted picture is PTS30, a reference P picture, required before central B pictures finish. It is included in emitted work. The helper fed 42 future pictures and discarded 41 queued pictures, so the study does not pretend that lookahead input was free. SATD, motion search and mbtree work performed while feeding those pictures is included in the measured encoder CPU.

Here encode+close CPU fell by about 48.9%, while emitted packets fell by 58.7%. The difference is consistent with retained lookahead work; packet count alone overstates CPU savings. Decoder work did not fall in this window because source GOP preroll and buffering still required the same actual source pictures. Adding a second process and raw pipe also has costs.

Setup wall/CPU, stdin read time and total helper CPU remain separate in the raw measurements. `stdin_read_seconds` includes pipe waiting and is not encoder CPU. The decoder's recorded observation-to-reap wall is an upper bound on its process completion time; it is not presented as isolated decoder execution time.

## Remaining continuous-encoding bias

The target is the actual same central presentation frame range from the full continuous CRF23 export. The metric retains VCL and other payload, removing only the identifiable x264 encoder-identification unregistered SEI with its known UUID; it does not remove arbitrary SEI or alter I frames. Thus zero-context results include the actual forced initial I picture. This is central packet payload evidence, not complete MP4-file accuracy or container overhead.

| Development source | 2s leading context error | 6s requested leading context error |
|---|---:|---:|
| static | 0.00% | 0.00% |
| motion | +1.96% | +0.44% |
| grain | +3.73% | +11.32% |
| brief-burst | +2.24% | +1.49% |
| cbr-burst | 0.00% | 0.00% |
| alternating | −0.36% | +0.20% |
| screen | +6.44% | +5.71% |
| pedestrians | +1.32% | −0.65% |
| megamind | −0.61% | 0.00% |

At zero context, pedestrians is +277.69% and Bunny is +89.06%. Bunny's 2s context is +0.281%; 6s is exact in this single window. One second of context for pedestrians gives +5.31%.

More preceding context is **not a monotonic accuracy improvement**: grain's 6s window is worse than its 2s window. Its continuous target is at global frames288–311; a full default-keyint250 encode can have a different periodic I-frame/reference phase than a restarted window. Prior references, GOP phase and mbtree propagation remain possible causes. This experiment does not assign the observed difference to generic CRF bitrate history or prove a correction formula.

Across the nine development sources, median direct-pipeline encode+close CPU ratios versus the same padded full encode were 0.540, 0.715 and 0.811 for 0/2/6s leading context. Median pipeline wall ratios were 0.872, 0.783 and 0.840. Cheap inputs have tiny CPU denominators and some single ratios exceed1; all measurements are retained. Neither these single-window medians nor the central errors are a statistical calibration guarantee.

## Budget implications

The existing estimator contract counts requested output-video encoding seconds. Closing libx264 early is a new backend with distinct decoded, fed/analyzed and emitted work; it cannot inherit that contract by counting only emitted packets.

| Pedestrians context | Source frames decoded | Fed/analyzed | Emitted | Fed fraction of whole source | Emitted fraction |
|---|---:|---:|---:|---:|---:|
| 1s before + 1s central | 183 | 62 | 21 | 15.5% | 5.25% |
| 2s before + 1s central | 185 | 72 | 31 | 18.0% | 7.75% |

Two independent pilots with the measured 1s/2s fed costs would require approximately 31%/36% of source frames fed; three would require 46.5%/54%. Those are arithmetic hypotheses, not measured second/third pilots. Consequently these measurements do **not** demonstrate that two or three independent pilots fit a 20% fed-work limit. The apparent three-pilot 15.75% from 1s emitted packets omits lookahead analysis and is invalid for such a limit. Decoder work in these same pilots was 45.75%/46.25% of source pictures due to preroll, so it needs its own accounting as well.

A deployment experiment should explicitly define and enforce bounds for decoded pictures, fed pictures, emitted packets, total CPU and wall, and cancellation before claiming a budget advantage. Under a separate CPU budget this mechanism may admit more pilots because it avoids transforming and entropy-encoding many future pictures; that hypothesis has not been certified against a continuous/full-export CPU baseline or an aggregate multi-pilot schedule. Shared decoding/state could change the totals and would require its own exactness and work proof.

## Cancellation and frontend matching

Eleven primary deadline tests expired after a 1ms helper wait. Both owned process groups were terminated and reaped in every case; the full and stop runs also left no owned child. `x264_encoder_close` returned with queued future pictures still present, without NULL-input flushing.

Normal early reader close sometimes causes FFmpeg 7.1.5 to exit 224 with `Broken pipe`; this is retained as a controlled reader-close observation, with zero source decode errors, not reported as normal FFmpeg completion. Full mode requires decoder exit 0. `wait4` measures decoder CPU even when an EPIPE path omits FFmpeg's final benchmark summary.

The strict native-source gate found real bridge mistakes before accepting additional windows:

- **Megamind SAR:** FFmpeg `setsar` defaults to `max=100`, approximating 705/704 as 1. This changed SPS. The driver now preserves the exact rational with `max=65535`; VCL packets had already matched.
- **Bunny VUI:** a raw bridge loses source SMPTE170m primaries/matrix, BT709 transfer and top-left chroma location. Applying only output metadata flags still left frame color properties unknown. The final bridge sets frame properties with `setparams`, preserves chroma location, and passes the same explicit H.264 VUI codes to x264. Full SPS/PPS now match the native-source export. No source pixels or target encoder settings were changed to improve accuracy.

The earlier blocked measurements remain available as evidence of why packet-byte similarity alone was not accepted as a complete encoder-contract match.

## Files

- [development-fixed/measurements.json](development-fixed/measurements.json): 27 initial fixed development windows; eight sources passed, initial Megamind windows were blocked on raw bridge SAR.
- [megamind-sar-proof/measurements.json](megamind-sar-proof/measurements.json): three accepted Megamind windows after preserving SAR; replaces those blocked rows in the aggregate.
- [bunny-vui-chroma-proof/measurements.json](bunny-vui-chroma-proof/measurements.json): three accepted Bunny windows after exact source VUI/chroma mapping.
- [pedestrians-one-second/measurements.json](pedestrians-one-second/measurements.json): additional fixed budget-hypothesis window.
- [decoded-central-proof.json](decoded-central-proof.json): independent actual full/stop central-frame decode comparisons, 31/31 equal.
- [final-driver-smoke/measurements.json](final-driver-smoke/measurements.json): current-driver regression run, including integrated decoded-central check and deadline cleanup.
- [summary.json](summary.json): aggregate gates, fixed-source errors, work counts, CPU/wall ratios and deadline outcomes. It excludes the explicitly blocked preliminary rows.

This is evidence for an exact, cheaper padded-probe backend under the tested configuration. It leaves the continuous reference/context problem, a new explicit budget contract, source access costs and broader encoder coverage as the next work.
