# Maintenance runtime and publication acceptance

This maintenance run passed native-component identity and complete export
publication checks. It does **not** rerun or improve the prior estimator
accuracy results. The source was frozen before this run and verified unchanged
afterward at
`b4440e98a293e38ffb1356a51068e1a75677ac0ba7e2e7417ec469d286f0ca3a`;
the old accuracy experiments remain attached to their recorded source hashes.

## Actual native pipeline fingerprint costs

Three separate calls to `native_pipeline_fingerprint` took **0.520581,
0.405537, and 0.432573 seconds** (median 0.432573 seconds). Each inspected the
actual FFmpeg and FFprobe process mappings and hashed **213 distinct native
components, totaling 228,416,872 bytes**. No application memoization was used.
The machine was shared with the final test suite; the OS page cache was not
cleared. The first call is therefore not a cold-OS measurement.

All three native pipeline fingerprints were identical:
`553261dc9244917147eeae4b30ceebca61ad96426324c50c76095a9cc6028382`.
The component-list digest was also identical:
`7a2d1116b6348d09a9e12c894637ee57829c6d609f313869e98ec9234df66842`.
These digests use the library's canonical compact sorted-key JSON encoding;
the component digest encodes `{"components": [...]}`. The complete component
names, byte lengths, and content hashes are saved in the three
`native-fingerprint-*.json` files. Their identity also matched the CLI's actual
toolchain key.

## One complete prepared CLI export

The existing 24-second static development fixture was used with libx264 CRF
23, `medium`, one thread, audio dropped, and the explicit segmented target of
six four-second segments. The CLI ran once with `--prepare --output`, a fresh
cache, `max_probes=0`, and no encoded-video fraction cap. This forces complete
export rather than low-budget sampled size estimation.

The CLI exited successfully with an exact, satisfied result and published
**24,590 bytes**, SHA-256
`a59b9e707f20af73cc4ea8aff2d52bdd2a21614ae231c271dfbd34cb3fdfe854`.
The cached artifact and published copy have the same independently checked
size and SHA. The full published output decoded normally with **576 frames**,
24 seconds, and zero duplicated or dropped frames. Its segment plan has six
96-frame segments; actual file inspection found six `moof`/`mdat` pairs.

The file's independently parsed container identity is exact:

```
734 init bytes + 14,064 video payload bytes + 9,792 fragment bytes = 24,590 bytes
```

There are no audio samples or audio payload bytes. The parsed fields agree
with the CLI's byte-accounting result.

| Measured CLI phase | Seconds | Scope |
|---|---:|---|
| Immutable input ingest | 0.00030047 | Outside Engine request budget |
| Engine request | 3.46450972 | Native fingerprint, segment preparation, all six encodes, mux and checks |
| First exact estimate | 3.46448981 | Complete actual export available; no sampled point estimate |
| Publication | 0.00189104 | Verified copy and exclusive atomic destination publication |
| CLI end to end | 3.46736199 | CLI internal timer, including ingest and publication |

The CLI reported 24 seconds of charged output-video work, or the entire
source. These results are operational checks, not a claim of prediction
accuracy at a low encoded fraction or improved industrial/SOTA performance.

## Verification correction and evidence

The initial maintenance verifier incorrectly required `nb_frames` in FFprobe
metadata. Fragmented MP4 metadata can omit that field; the CLI and full decode
had already succeeded. The verifier was corrected to require the actual
complete decode count and to check metadata frame count only when provided.
Postprocessing resumed from the saved evidence without another fingerprint
trial, encoding, publication, or decode. An independent read-only review also
checked the hashes, boxes, frame count, source freeze, and timing fields.

The original verifier failure and runner checksum are preserved in
`verification-failure.json`; its original source is in the ignored cache.
The raw CLI phase timings survived. The original outer Python-process and
decode wall timings were not persisted before that verifier error and remain
explicitly null in `summary.json`. They are not reconstructed from frame rates
or substituted with another run.

`summary.json` is the final acceptance result. `cli-stdout.json`, the three
component lists, full decode progress, independent box accounting, source
snapshot checksum, and request/command files retain the underlying evidence.
Encoded artifacts remain in ignored `.vsize-cache/`. No old accuracy report or
reserved-source prediction was modified or rerun.
