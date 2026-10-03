# Global preview and rare-content discovery: development study

All nine frozen development cases were tested. No reserved industrial-video accuracy labels were opened. The study measures global output-space previews at 1/2/4/8fps and 96×54 grayscale, using metadata-only time blocks without scanning source packet metadata. Each strategy uses exactly three padded context probes, plus up to three independently randomized audit probes.

## What changed in the result

A 4fps or 8fps full-content preview finds rare-content strata that three uniform probes miss. It turns the brief-burst and constant-input-bitrate burst payload errors from approximately -99%/-98% into -0.629%/+0.938%. This is a discovery improvement on development data, not a calibrated coverage or SOTA result.

| Case | Uniform3 raw payload | Mean4 raw payload | Mean8 raw payload | Mean8 optional periodic I | Mean8 full-file error with old mux model |
|---|---:|---:|---:|---:|---:|
| static | -1.449% | -0.686% | -0.686% | +0.308% | -12.809% |
| motion | -4.208% | +0.218% | +0.218% | +1.117% | -0.025% |
| grain | +1.283% | -1.604% | -1.604% | -1.338% | -1.642% |
| brief-burst | -99.267% | -0.629% | -0.629% | -0.629% | -0.761% |
| cbr-burst | -98.396% | +0.938% | +0.938% | +0.938% | +0.587% |
| alternating | -80.794% | -3.484% | -3.636% | -2.952% | -3.664% |
| screen | -6.954% | -5.562% | -5.562% | +0.699% | -7.126% |
| pedestrians | -9.616% | -3.737% | -3.737% | +5.026% | -3.884% |
| megamind | +0.492% | -3.187% | +0.492% | +0.492% | +0.338% |

Raw payload contains sampled video content plus x264 initialization SEI added once. The optional periodic-I candidate comes from the context adapter and is scored separately. Whole-file columns use the existing `2048 + 6×estimated_frames` mux model, so they deliberately retain the known MP4 sample-table underestimation; complete-reference container bytes are never substituted into predictions.

## Costs are included

| Case | Mean8 global preview | Mean8 metadata + preview + 3 warm probes | Existing full encode | Warm encoding / source duration |
|---|---:|---:|---:|---:|
| static | 0.399s | 2.142s | 0.462s | 0.651 |
| motion | 0.369s | 3.120s | 1.847s | 0.573 |
| grain | 1.880s | 9.739s | 7.907s | 0.573 |
| brief-burst | 0.336s | 4.761s | 1.777s | 0.651 |
| cbr-burst | 0.203s | 4.062s | 1.538s | 0.651 |
| alternating | 0.804s | 6.087s | 5.353s | 0.734 |
| screen | 0.183s | 2.038s | 1.344s | 0.651 |
| pedestrians | 0.286s | 2.850s | 2.058s | 0.625 |
| megamind | 0.252s | 3.226s | 1.840s | 1.344 |

Each strategy is charged its own complete preview preparation and the recorded cold wall time of each selected probe, even though experiments share measured probes to avoid repeating identical expensive work. Actual padded media duration is counted, not merely the central two-second windows. Preview is a complete source decode at the source resolution before temporal dropping; reducing output FPS does not eliminate its input decode cost.

This is a costed replay with descriptive timings on a shared CPU, not a randomized common-deadline benchmark. Full-reference timings were measured by the preceding component benchmark. On these short low-resolution clips, all mean8 three-probe routes cost more than the full encode. The engine should retain a full-encode cost fallback. Preparation can be amortized across repeated settings on a long immutable asset, but its first-build cost must remain visible.

## Counterexamples and limitations

- Adding max texture/change features is not automatically better. At 4fps it changes alternating-content strata enough to produce -30.374% raw-payload error, versus -3.484% with mean features. At 8fps, max features worsen the nine-case worst error from 5.562% to 8.974%.
- One FPS yields -24.875% on brief-burst and -17.286% on cbr-burst despite the global scan. Sparse temporal samples distort how much of a block contains the rare content.
- The half-second phase diagnostic uses actual decoded development noise frames and a hypothetical active interval [10,10.5). It is separate from video accuracy. One FPS misses 12 of 24 sampling phases; 2/4/8fps detect all phases of this particular half-second event. Any finite fixed FPS can miss a shorter event; maxima cannot recover a frame that was never observed.
- Low-resolution grayscale also loses fine spatial and chroma complexity. Discovering these nine events does not establish universal discovery of industrial footage.
- More randomized audits need not monotonically improve a single realized estimate. All prefixes from three to six probes are reported, including increases in error; at six probes megamind is excluded because its metadata plan contains only five blocks.
- Context correction and the mux model remain distinct error sources. For pedestrians, raw mean8 payload is -3.737% but optional periodic correction becomes +5.026%; for screen the corresponding errors are -5.562% and +0.699%. A universal correction is not supported by these examples.
- Selection uses only decoded preview features. Complete references are opened only after candidate predictions are persisted, solely for scoring. Development feedback was used to formulate these candidates; these results are not untouched generalization evidence.

## Reproduce and inspect

```bash
.venv/bin/python benchmarks/discovery_study.py
```

`metadata.json` records the manifest/script digests, encoder contract, preview rates and seed. `*-previews.json` contain actual FFmpeg commands, preparation times, preview frame counts and all block features. `measurements/` contain the real padded context-probe measurements. `predictions.jsonl` is persisted before scoring; `evaluated.json`, `summary.json` and `comparison.csv` include every method and audit prefix. The source and complete-reference media digests are checked, and reused probe artifacts are validated through the ordinary content cache.

Implementation reuses `Media.metadata_blocks` and captures the actual 8fps `Media._visual_features` raw output once; peak statistics reuse those frames rather than paying for a second 8fps decode. No root runtime module was changed by this study.
