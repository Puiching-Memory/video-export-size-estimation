# Development experiment: short-encode context bias

All nine frozen development cases were measured. No `test_reserved` accuracy was opened. This is a deliberately expensive census of every two-second block: it isolates encoder-context bias, not sampling uncertainty or practical speed.

All fresh outputs use libx264, CRF 23, medium, yuv420p, one encoder thread, no audio, MP4 faststart, and no scaling or FPS conversion. Complete references use the same settings; the older two-thread references are not used for scoring. Exact commands, codec metadata, packet positions, I/P/B decomposition and stream payload totals are stored beside the encoded artifacts under `artifacts/context-study/`.

Candidate measurements are persisted before the complete reference is encoded/read for scoring. The experimental candidate parameters were refined on development results; this is not an untouched validation result.

## Signed error of complete block census

`Old MP4` compares complete file bytes. All other columns compare video packet payload bytes, excluding container bytes. Different targets are shown explicitly; these are not final file-size accuracy claims.

| Case | Old MP4 | Clip payload | Drop startup I + periodic guess | Warm 2s/1s | Warm + adequate lookahead | Warm + periodic I excluding SEI |
|---|---:|---:|---:|---:|---:|---:|
| static | +96.090% | +76.282% | +7.476% | -1.001% | -1.001% | +0.036% |
| motion | +4.494% | +3.398% | -1.930% | -1.114% | -1.085% | -0.149% |
| grain | +0.588% | +0.430% | -0.920% | -0.092% | -0.030% | +0.225% |
| brief-burst | +0.626% | -0.045% | -0.549% | -0.654% | -0.654% | -0.647% |
| cbr-burst | +2.392% | +0.936% | -1.234% | +0.883% | +0.883% | +1.117% |
| alternating | -3.229% | -3.481% | -4.540% | -3.912% | -3.908% | -3.722% |
| screen | +32.939% | +26.549% | -8.613% | -6.230% | -6.207% | -0.214% |
| pedestrians | +68.817% | +66.688% | -27.922% | -18.322% | -4.954% | +0.070% |
| megamind | +8.077% | +6.511% | -5.039% | +0.081% | +0.131% | +2.204% |

Adequate-lookahead settings here are 2s before/2s after for 24fps cases, and 2s before/5s after for the 10fps pedestrians case. x264 medium reports `rc_lookahead=40`, `bframes=3`, `keyint=250`, `mbtree=1` in actual encoder logs.

## What the experiments establish

- MP4 fixed overhead and the forced startup I frame can produce large errors even when every source block is observed. Increasing sample count does not remove this bias.
- The first packet contains a 690-byte x264 initialization SEI in these outputs (686-byte NAL plus four-byte AVCC length). Normal periodic I frames do not repeat it. Treating this metadata as periodic I content causes a 13.192% static-payload error; removing SEI reduces that candidate to +0.036%.
- Medium lookahead is measured in frames. One second of lookahead at 10fps supplies only ten of the required forty frames. Extending pedestrians future context from 1s to 5s reduces central-payload error from -18.322% to -4.954%. Periodic I compensation excluding SEI further reaches +0.070% on this development case.
- Blind periodic I compensation fails on megamind: its warmed central windows already contain all four reference I frames due to scene cuts. Raw warmed payload has +0.131% error; blindly adding periodic I increases it to +2.204%.
- Alternating remains -3.908% with 2s/2s padding. Its 8–10s window has twelve P / thirty-five B frames in the probe versus forty-seven P / zero B in the reference, showing that differing GOP and historical encoder state change normal frame costs too. Increasing only leading context to 6s reduces whole-census error to -0.472%; this is not a proof that any finite padding is unbiased.
- Container bytes, video payload and any future audio model require separate treatment. This corpus has no audio; audio behavior is tested separately by the new context-probe integration tests.

No confidence interval, industrial acceptance or SOTA claim follows from these nine development cases. Warmed artifacts are measurements, not final continuous exports.

## Reproduce

```bash
.venv/bin/python benchmarks/context_study.py --selftest
.venv/bin/python benchmarks/context_study.py --cases static,motion,grain,brief-burst,cbr-burst,alternating,screen,pedestrians,megamind
.venv/bin/python benchmarks/context_study.py --cases static,motion,grain,brief-burst,cbr-burst,alternating,screen,megamind --before 2 --after 2 --output results/context-study-2026-10-03-lookahead-24fps
.venv/bin/python benchmarks/context_study.py --cases pedestrians --before 2 --after 5 --output results/context-study-2026-10-03-lookahead
.venv/bin/python benchmarks/context_study.py --cases pedestrians --before 5 --after 5 --output results/context-study-2026-10-03-long-context
.venv/bin/python benchmarks/context_study.py --cases alternating --before 6 --after 2 --output results/context-study-2026-10-03-alternating-memory
```

The script rejects any source not in the frozen development manifest, verifies source and cached-output SHA-256, joins frames and packets by packet file position, and uses PTS half-open intervals so B-frame decode order does not affect central-window assignment. Seven self-test assertions cover PTS selection, I/P/B accounting, startup correction and AVCC truncation/SEI inspection.

## Runtime adapter and verification

`src/vsize/context_probe.py` supplies `context_window(media, window)` and `probe(media, crf, window)` without changing the final continuous export contract. The policy fingerprints the preset, effective output FPS, two-second leading context, lookahead/B delay/guard frames, keyint and packet/SEI accounting. The nine preset lookahead/Bframe mappings were checked against real encoder logs; the extracted values are in `preset-policy-verification.json`.

The adapter returns raw central packets, content payload after excluding initialization SEI, separate video/audio totals, raw and content I/P/B byte rates, and a separate optional periodic-I model candidate. The latter uses `max(0, output_fps/250 - observed_central_I_rate)` times central duration times the first-I excess after removing initialization SEI. Observed scene I frames suppress this candidate instead of being blindly counted again. This remains a model hypothesis because finite sampling can miss scene cuts and encoder history.

A final payload total must add `global_init_sei_bytes` once; `global_audio_priming_bytes` exposes AAC leading packets separately. Audio assignment uses the same half-open PTS interval, so AAC packet boundary quantization still belongs to model uncertainty. The source-average FPS fallback does not guarantee future frame coverage for VFR footage. Actual future frame count and truncation at the source tail are reported.

`context_window.encoded_media_seconds` is a preflight padded input span. `probe.encoded_media_seconds` is observed encoded-video packet duration; the requested span remains available separately. All padding counts towards compute use. `warmup_artifact` is deliberately separate from a final `artifact` field.

Five integration tests passed in `context-probe-tests.txt`: low-FPS lookahead and output-FPS-filter policies, bounds and source tail, central I/P/B and initialization SEI separation, first-audio retention verified by a 440Hz spectral peak against an unselected 880Hz track, audio dropping, true warmup cost, and reuse of an intact warmup cache artifact. These tests verify adapter semantics, not prediction coverage or SOTA.
