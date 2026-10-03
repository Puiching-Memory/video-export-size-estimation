# libx264 streaming context research

This is a development-only experiment, separate from the frozen estimator and its budget contract. It proves an exact stopping rule for the tested encoder configuration, measures its work, and checks remaining continuous-encoding bias. It does not implement a production backend or infer that emitted packets alone satisfy `max_encode_fraction`.

`x264_context.c` links the exact shared libx264 used by FFmpeg. It consumes raw I420, feeds frames with a frame-clock PTS, records each AVCC packet, and either flushes the full input or closes without flushing after every central PTS has actually been emitted. A future P or reference B packet can precede the last central B packet; those packets are retained and counted.

`study.py` requires a frozen source snapshot. Create one before subsequent estimator changes:

```sh
mkdir -p artifacts/stream-context-f39/src
cp -R src/vsize artifacts/stream-context-f39/src/
```

The experiment in the linked report used the 16 Python files from frozen v2 `d300d1f`, fingerprint prefix `f39`, copied before the v2.1 maintenance changes. The driver records every snapshot file hash and imports that snapshot, even when the working source changes. To reproduce with a different explicit snapshot, set `VSIZE_FROZEN_SOURCE_ROOT`.

The installed runtime was Debian `libx264-164` `2:0.164.3108+git31e19f9-2+b1`. The exact matching public development package was downloaded and extracted under `/tmp/vsize-x264-headers/extracted`, without installing packages into the system:

```sh
curl -fL https://deb.debian.org/debian/pool/main/x/x264/libx264-dev_0.164.3108+git31e19f9-2+b1_amd64.deb -o /tmp/libx264-dev.deb
dpkg-deb -x /tmp/libx264-dev.deb /tmp/vsize-x264-headers/extracted
```

Check the local runtime revision first; a different shared-library build requires its own matching header and new equivalence proof. The Python driver compiles the C helper with `gcc`, records the header/library hashes, and links `/lib/x86_64-linux-gnu/libx264.so.164` directly.

```sh
.venv/bin/python benchmarks/stream_context_study/study.py \
  --output results/stream-context-study-repeat \
  --cases static motion grain brief-burst cbr-burst alternating screen pedestrians megamind \
  --leading 0 2 6
```

Output and artifact directories must be fresh. Source selection is restricted to the old development manifest; `--cases bunny` uses the previously inspected `/tmp/video-estimation-research/bunny-full.mkv`. Neither the reserved corpus nor holdout accuracy results are consulted.

Each window must pass source-pixel equality against the same continuous decoded frame range, API-full equality against raw FFmpeg and native-source padded FFmpeg, and central API-stop equality against API-full. The checks cover AVCC bytes, PTS/DTS, SPS/PPS and decoded central pixels. The raw bridge explicitly preserves exact SAR and source color/chroma metadata; incomplete matches are reported as blocked.

CPU accounting uses the helper's process CPU clock for encoder calls, including lookahead and close, a separate setup/process total, and `wait4` for decoder CPU even when the raw pipe closes with EPIPE. Source parsed packets, decoded frames, raw frames emitted, frames fed to x264, packets emitted and discarded queued frames remain separate. Pipe times are observed completion/reap times, not isolated decoder execution times. Logs use debug and benchmark instrumentation; single wall measurements under concurrent cloud work are not latency guarantees.

Both process groups are terminated and reaped on deadline expiry. A controlled FFmpeg 7.1.5 EPIPE exit must be code 224, contain `Broken pipe`, and lack a reported independent decode failure; it is retained in the evidence. Successful full mode requires normal decoder exit.

`verify_decoded.py` independently re-decodes saved full and stop AnnexB artifacts without new encoding:

```sh
.venv/bin/python benchmarks/stream_context_study/verify_decoded.py \
  --output results/stream-context-study-repeat/decoded-proof.json \
  results/stream-context-study-repeat/measurements.json
```

The measured report is [results/stream-context-study-2026-10-03/README.md](../../results/stream-context-study-2026-10-03/README.md).
