# Frozen v2 long-source final evaluation

The full long-source test completed, but the frozen estimator did not meet a
reliable long-export acceptance criterion. Temporal sampling's final error was
−8.73% at 30 seconds and **+90.51% at 60 seconds**; both content requests spent
their wall budgets on the whole-source preview and returned no estimate. No
request satisfied the calibrated reliability contract. Increasing the budget
did not improve this source's final point accuracy.

This run uses the entire original Meridian SDR source: 718.933333 seconds of
3840×2160 video at 60000/1001 fps, 43,093 source video frames, and 1,299,914,020
input bytes. It is one `test_reserved` source group (`meridian-2016`). Previous
work measured three throughput windows, without generating this full target
label. The current predictions were saved before the full reference began.

The output target is **640×360 at 12 fps**, continuous MP4, libx264 CRF 23,
`veryfast`, yuv420p, one codec/filter thread, with audio dropped. This tests a
long original input and an explicitly downscaled export; it is not a native 4K
output result.

The library is frozen at
`f39dca1c9f04afec908f1171daa898e2bf4c3fa82073cdb5908d8b9eec562ee2`.
The original input SHA-256 is
`526b5dad800cdbd7f208bd35d717928bda0ee712a2dd21a6326a43a80717e496`.
Each stage preserves the source and runner snapshot in its ignored cache.

## Predictions fixed before labels

Each request starts with an independent empty cache, six maximum probes,
four-second central samples, `probe_mode=auto`, the same seed, and a maximum
charged output-video fraction of 20%. A single immutable prepared input costs
2.228 seconds to ingest, outside each request's wall budget. Preparation reuses
input bytes, not content features or encoded samples.

| Plan | Wall budget | First estimate | Final point estimate | Signed error | Completed padded video | Charged attempted video | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| temporal | 30 s | 17.028 s | 14,571,781 B | −8.73% | 7.25 s / 1.008% | 14.5 s / 2.017% | budget exhausted; uncalibrated point retained |
| content | 30 s | — | — | — | 0 | 0 | whole preview timed out; no estimate |
| temporal | 60 s | 15.819 s | 30,414,776 B | +90.51% | 36.25 s / 5.042% | 43.5 s / 6.051% | budget exhausted; uncalibrated point retained |
| content | 60 s | — | — | — | 0 | 0 | whole preview timed out; no estimate |

The content previews spent 29.825 and 59.841 seconds before cancellation.
These failed preview costs count against wall time, even though no video size
probe was encoded. An encoded-video fraction alone therefore understates this
strategy's resource cost. Temporal completed one and five padded exports;
each also started one further export before cancellation. The actual partial
output of those cancelled attempts is unknown; charged spans conservatively
include their full requested video duration. Neither plan received cached
features from another request. None of the four requests satisfied the
requested calibrated reliability contract.

Prediction files and completion SHA are in `predictions/`. The diagnostic
`completed-probe-work.json` lists the successfully published probe exports;
its durations include context padding and exclude unknown cancelled partials.
The fixed-budget request outcomes are two estimates and two no-estimate
refusals; one of all four requests has a point within 10%. These fractions
describe the four requests for one source, not population accuracy or coverage.
There were no charged-video fraction violations. Maximum measured wall-budget
overshoot was 0.0205 seconds, including cancellation and cleanup.

All emitted prefixes were also saved before labels. The 60-second trajectory
had signed errors −8.73%, +91.62%, +30.40%, +30.07%, and +90.51% at 15.82,
29.87, 36.50, 43.94, and 55.01 seconds. The score uses the final saved estimate
at the requested budget; it does not select the best prefix after seeing the
reference. The raw prefix scores are in `prefix-evaluation.json`.

## Complete reference

The unique full reference completed with `Media.encode(23)` without a window
and a 1,800-second deadline. Its output contains 8,627 frames and lasts
718.916667 seconds; the 0.016666-second difference from the original video's
duration is within the declared 12-fps rounding tolerance. The output is H.264
yuv420p, 640×360 at 12 fps, with no audio. Full decoding completed normally and
counted all 8,627 output frames.

The complete output is **15,965,193 bytes** (15,862,784 packet payload bytes),
SHA-256
`88b5e290d353ad1778985f362b0cb70c57c169ce3c16c5f67d89b4b799cd2fee`.
Encoding and output inspection took 971.095 seconds; FFmpeg's internal
transcode time was 970.791 seconds. The whole reference encode stage took
971.323 seconds, excluding its separately measured 2.225-second immutable
input ingest. Full output decode took 5.243 seconds, outside that encode-stage
deadline. Size, SHA, duration, frame count, and export-setting verification all
passed. There was one reference encoding, no truncated output or retry.

The encoder adds only observational `-progress` and `-stats_period` arguments.
The command, progress, metadata, code snapshot, and success/failure status are
preserved under `reference/`; encoded MP4s stay in ignored `.vsize-cache/`.
Reference decode and verification use their own deadlines after the encode.
Thirty-second resource observations began about 2.8 minutes into encoding and
are saved in `resource-observations.jsonl`. They observed at most two codec
processes and a maximum reference FFmpeg RSS of 215,715,840 bytes. This is a
sampled process value, not a full-run memory upper bound. The 1.30-GB sealed
input's kernel pages are additional; shared-cgroup memory also contains other
tasks and page cache. No isolated latency or per-request cgroup peak is claimed.

## Reproduction and limits

From the repository root, with the exact original input already downloaded:

```sh
PYTHONPATH=src .venv/bin/python benchmarks/long_core_validation.py predict --output results/industrial-long-reproduction
PYTHONPATH=src .venv/bin/python benchmarks/long_core_validation.py reference --output results/industrial-long-reproduction
```

The second command refuses to begin before all four predictions have been
saved. Existing prediction directories and existing reference stages are
refused rather than overwritten. `all` runs both stages in order in one
process. The library source hash must match the freeze for every run.

Timings are from a shared four-core cloud CPU quota, without flushing the OS
page cache. Codec competitors are observed at request/stage starts; this does
not establish isolated latency. This is one long held-out group and two
uncalibrated point-estimate strategies. It cannot establish population
coverage, industrial throughput across resolutions, or SOTA performance.
