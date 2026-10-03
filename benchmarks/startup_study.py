"""Development-only costed replay of independent-probe startup corrections.

The candidate formula is written before measurement and cannot use reference
bytes. Existing matching one-second artifacts may be reused, with their original
cold encoding time charged. This is not a fresh common-deadline benchmark.
"""

import argparse
import copy
import csv
import json
import math
import statistics
import subprocess
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from vsize import EncodeSpec
from vsize.fmp4 import read_track
from vsize.inference import Sampler
from vsize.media import Media, PIPELINE_VERSION
from vsize.mux_model import estimate_video_container, inspect_video_container
from vsize.runtime import Cache, Deadline, canonical_key, digest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "results/development-2026-10-02/manifest.json"
EXISTING = ROOT / "results/core-validation-v2-2026-10-03/engine/.vsize-cache"
REFERENCES = ROOT / "results/core-validation-v2-2026-10-03/engine/continuous-references.json"
SEED = 20261003
FRACTIONS = (0.2, 0.25)
METHODS = ("uncorrected", "deSEI_only", "deSEI_startupI_periodic")
FORMULA = {
    "version": "development-startup-packet-candidate-v1",
    "selection": "frozen_v2_content_4fps_mean_features_and_seeded_random_audits",
    "block_seconds": 1.0,
    "pilot_count": "min(3, blocks, 6, max(1, floor(fraction * duration / mean_block_duration)))",
    "max_probes": 6,
    "seed": SEED,
    "fractions": list(FRACTIONS),
    "sei": "whole first-packet AVCC NAL only if NAL=6 and all SEI messages have type=5",
    "nonkey_mean": "arithmetic mean of bytes of all non-keyframe packets in that probe",
    "startup_excess": "E_i=max(0, first_keyframe_bytes - first_packet_user_SEI_bytes - nonkey_mean_i)",
    "uncorrected": "Sampler(raw_payload)",
    "deSEI_only": "Sampler(raw_payload - user_SEI) + max(observed_user_SEI)",
    "deSEI_startupI_periodic": (
        "Sampler(raw_payload - user_SEI - E_i) + max(observed_user_SEI) + "
        "max(1, ceil(metadata_frames / 250) - max(0, Sampler(nonstartup_keyframe_count))) "
        "* mean(observed_E_i)"
    ),
    "metadata_frames": "max(1, round(source_video_duration * source_average_fps))",
    "keyframe_container_count": (
        "min(metadata_frames, max(ceil(metadata_frames/250), "
        "1 + round(max(0, Sampler(nonstartup_keyframe_count)))))"
    ),
    "container": "mean(mux_model.estimate_video_container(each_observed_census, metadata_frames, keyframe_container_count))",
    "container_comparison": "all three candidates use the same predicted container; frozen_v2_default_container also recorded",
    "cost": (
        "actual source metadata/hash setup + cold whole-source preview + sum(original cold encode_wall_seconds "
        "+ current packet/census diagnostic and cache lookup wall_seconds) + candidate inference CPU; "
        "reused artifacts are costed replay; benchmark JSON/log writes and final verification excluded"
    ),
    "limitations": [
        "Independent x264 initial QP, B decisions, reference state and lookahead remain different.",
        "Startup I excess estimates periodic I cost approximately; sampled scene-cut keyframes can differ.",
        "A global periodic count and unweighted observed excess mean need not match content at true keyframe times.",
        "Cold timing replay is approximate, not a hard deadline outcome or calibrated confidence interval.",
        "Only the original nine development cases are permitted; no SOTA or heldout claim.",
    ],
}


class RecordingDeadline(Deadline):
    def __init__(self, seconds):
        super().__init__(seconds)
        self.commands = []

    def run(self, command):
        started = time.monotonic()
        result = super().run(command)
        self.commands.append({"command": command, "wall_seconds": time.monotonic() - started})
        return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def rbsp(data):
    """Remove valid H.264 emulation-prevention bytes without deleting real data."""
    output = bytearray()
    zeroes = 0
    for index, value in enumerate(data):
        if zeroes >= 2 and value == 3 and index + 1 < len(data) and data[index + 1] <= 3:
            zeroes = 0
            continue
        output.append(value)
        zeroes = zeroes + 1 if value == 0 else 0
    return bytes(output)


def sei_messages(nal):
    raw = rbsp(nal[1:])
    offset = 0
    messages = []
    while offset < len(raw):
        if raw[offset:] == b"\x80":
            break
        values = []
        for _ in range(2):
            value = 0
            while offset < len(raw) and raw[offset] == 255:
                value += 255
                offset += 1
            if offset >= len(raw):
                raise ValueError("truncated SEI type or size")
            value += raw[offset]
            offset += 1
            values.append(value)
        kind, size = values
        if offset + size > len(raw):
            raise ValueError("truncated SEI message")
        body = raw[offset : offset + size]
        offset += size
        item = {"payload_type": kind, "payload_bytes": size}
        if kind == 5 and size >= 16:
            item["uuid_hex"] = body[:16].hex()
            item["user_data_text"] = body[16:].decode("utf-8", errors="replace").rstrip("\x00")
        messages.append(item)
    return messages


def packet_nals(packet, length_bytes):
    offset = 0
    result = []
    while offset < len(packet.data):
        if offset + length_bytes > len(packet.data):
            raise ValueError("truncated AVCC length")
        size = int.from_bytes(packet.data[offset : offset + length_bytes], "big")
        offset += length_bytes
        if size <= 0 or offset + size > len(packet.data):
            raise ValueError("invalid AVCC NAL size")
        nal = packet.data[offset : offset + size]
        offset += size
        kind = nal[0] & 31
        item = {"nal_type": kind, "avcc_bytes": size + length_bytes}
        if kind == 6:
            item["sei_messages"] = sei_messages(nal)
        if kind in (1, 5):
            item["picture_type"] = slice_picture_type(nal)
        result.append(item)
    return result


def slice_picture_type(nal):
    """Read the two initial Exp-Golomb fields without decoding picture pixels."""
    raw = rbsp(nal[1:])
    at = 0

    def bit():
        nonlocal at
        if at >= len(raw) * 8:
            raise ValueError("truncated slice header")
        value = (raw[at // 8] >> (7 - at % 8)) & 1
        at += 1
        return value

    def unsigned_exp_golomb():
        zeroes = 0
        while bit() == 0:
            zeroes += 1
            if zeroes > 31:
                raise ValueError("unreasonable slice header")
        suffix = 0
        for _ in range(zeroes):
            suffix = suffix * 2 + bit()
        return (1 << zeroes) - 1 + suffix

    unsigned_exp_golomb()  # first_mb_in_slice
    value = unsigned_exp_golomb()  # slice_type
    if value > 9:
        raise ValueError("invalid H.264 slice_type")
    return ("P", "B", "I", "SP", "SI")[value % 5]


def diagnose(record, deadline):
    started = time.monotonic()
    track, packets = read_track(Path(record["artifact"]), deadline=deadline)
    if track.codec != "h264" or len(track.extradata) < 5 or track.extradata[0] != 1:
        raise ValueError("AVCC H.264 is required")
    if not packets or not packets[0].keyframe:
        raise ValueError("independent probe must begin with a keyframe")
    length_bytes = (track.extradata[4] & 3) + 1
    first_nals = packet_nals(packets[0], length_bytes)
    sei = sum(
        nal["avcc_bytes"]
        for nal in first_nals
        if nal["nal_type"] == 6
        and nal["sei_messages"]
        and all(message["payload_type"] == 5 for message in nal["sei_messages"])
    )
    nonkeys = [len(packet.data) for packet in packets if not packet.keyframe]
    if not nonkeys:
        raise ValueError("cannot infer startup excess without non-keyframe packets")
    mean = statistics.fmean(nonkeys)
    excess = max(0.0, len(packets[0].data) - sei - mean)
    payload = sum(len(packet.data) for packet in packets)
    if payload != record["payload_bytes"] or len(packets) != record["video_frames"]:
        raise ValueError("cached byte accounting disagrees with AVCC packets")
    census = inspect_video_container(record["artifact"], deadline)
    if census is None or census["payload_bytes"] != payload:
        raise ValueError("unsupported or inconsistent development MP4 container")
    packet_rows = []
    for index, packet in enumerate(packets):
        nals = packet_nals(packet, length_bytes)
        picture_types = sorted({n["picture_type"] for n in nals if "picture_type" in n})
        if len(picture_types) != 1:
            raise ValueError("mixed or missing picture type in a video packet")
        packet_rows.append(
            {
                "decode_index": index,
                "pts": packet.pts,
                "dts": packet.dts,
                "duration_ticks": packet.duration,
                "bytes": len(packet.data),
                "keyframe": packet.keyframe,
                "nal_types": [n["nal_type"] for n in nals],
                "picture_type": picture_types[0],
            }
        )
    by_type = {}
    for kind in ("I", "P", "B", "SP", "SI"):
        sizes = [p["bytes"] for p in packet_rows if p["picture_type"] == kind]
        by_type[kind] = {
            "count": len(sizes),
            "payload_bytes": sum(sizes),
            "mean_bytes": statistics.fmean(sizes) if sizes else None,
        }
    actual_seconds = float(
        (max(p.pts + p.duration for p in packets) - min(p.pts for p in packets)) * track.time_base
    )
    return {
        **record,
        "avcc_length_bytes": length_bytes,
        "time_base": str(track.time_base),
        "first_packet_nals": first_nals,
        "first_keyframe_bytes": len(packets[0].data),
        "startup_user_sei_bytes": sei,
        "nonkey_count": len(nonkeys),
        "nonkey_mean_bytes": mean,
        "startup_i_excess_bytes": excess,
        "nonstartup_keyframe_count": sum(p.keyframe for p in packets[1:]),
        "deSEI_payload_bytes": payload - sei,
        "deSEI_startupI_payload_bytes": payload - sei - excess,
        "packets": packet_rows,
        "by_picture_type": by_type,
        "mux_census": census,
        "actual_encoded_media_seconds": actual_seconds,
        "diagnostic_wall_seconds": time.monotonic() - started,
    }


def existing_probe(media, case_id, window):
    key = media.key(23.0, window)
    # Explicit development-only directories. Never search a holdout cache.
    for lifecycle in ("prepared", "cold-original"):
        for fraction in ("0.2", "0.5"):
            root = EXISTING / f"{case_id}-continuous-{lifecycle}-5.0-{fraction}"
            if root.is_dir():
                record = Cache(root).lookup(key, media.deadline)
                if record:
                    if record["source_hash"] != media.source_hash:
                        raise ValueError("wrong development source in reusable cache")
                    if record["configuration"] != media.spec.configuration():
                        raise ValueError("wrong export configuration in reusable cache")
                    return record
    return None


def scalar_estimate(sampler, measured, indices, field):
    selected = copy.deepcopy(sampler)
    for index in indices:
        selected.add(index, {**measured[index], "payload_bytes": measured[index][field]})
    return selected.estimate()["estimated_payload_bytes"]


def predict(sampler, measured, indices, media, setup, discovery, fraction, pilot_count):
    inference_started = time.monotonic()
    selected = [measured[index] for index in indices]
    frames = max(1, round(media.duration * media.fps))
    periodic_count = math.ceil(frames / 250)
    nonstartup = max(0.0, scalar_estimate(sampler, measured, indices, "nonstartup_keyframe_count"))
    needed = max(1.0, periodic_count - nonstartup)
    excess = statistics.fmean(r["startup_i_excess_bytes"] for r in selected)
    global_sei = max(r["startup_user_sei_bytes"] for r in selected)
    keyframes = min(frames, max(periodic_count, 1 + round(nonstartup)))
    container = statistics.fmean(
        estimate_video_container(r["mux_census"], frames, keyframe_count=keyframes)
        for r in selected
    )
    v2_container = statistics.fmean(
        estimate_video_container(r["mux_census"], frames) for r in selected
    )
    raw = scalar_estimate(sampler, measured, indices, "payload_bytes")
    de_sei = scalar_estimate(sampler, measured, indices, "deSEI_payload_bytes") + global_sei
    de_i_base = scalar_estimate(sampler, measured, indices, "deSEI_startupI_payload_bytes")
    payloads = (raw, de_sei, de_i_base + global_sei + needed * excess)
    reserved_seconds = sum(sampler.blocks[i]["duration"] for i in indices)
    cold_encode = sum(r["encode_wall_seconds"] for r in selected)
    diagnostics = sum(r["diagnostic_wall_seconds"] for r in selected)
    lookup = sum(r["cache_lookup_wall_seconds"] for r in selected)
    inference_wall = time.monotonic() - inference_started
    cost = (
        setup
        + discovery["cold_elapsed_seconds"]
        + cold_encode
        + diagnostics
        + lookup
        + inference_wall
    )
    shared = {
        "phase": "prediction",
        "prediction_uses_reference": False,
        "sample_count": len(indices),
        "requested_pilot_count": pilot_count,
        "pilot_indices": sampler.pilots,
        "observed_indices": indices,
        "all_pilots_observed": all(i in indices for i in sampler.pilots),
        "audit_indices": [i for i in indices if i in sampler.audit_order],
        "strata": sampler.groups,
        "budget_fraction": fraction,
        "reserved_encode_seconds": reserved_seconds,
        "actual_encoded_media_seconds": sum(r["actual_encoded_media_seconds"] for r in selected),
        "encoded_fraction": reserved_seconds / media.duration,
        "within_encode_budget": reserved_seconds <= fraction * media.duration + 1e-7,
        "setup_wall_seconds": setup,
        "whole_source_preview_cold_wall_seconds": discovery["cold_elapsed_seconds"],
        "whole_source_preview_decoded_source_seconds": media.duration,
        "cold_encode_wall_seconds": cold_encode,
        "packet_diagnostic_wall_seconds": diagnostics,
        "cache_lookup_wall_seconds": lookup,
        "candidate_inference_wall_seconds": inference_wall,
        "costed_total_wall_seconds": cost,
        "within_replayed_5s_wall_budget": cost <= 5,
        "reused_probe_count": sum(r["artifact_reused_from_v2"] for r in selected),
        "metadata_full_frames": frames,
        "periodic_keyframe_count": periodic_count,
        "estimated_nonstartup_keyframe_count": nonstartup,
        "startup_i_excess_observed_mean": excess,
        "periodic_excess_keyframe_count": needed,
        "periodic_i_excess_bytes": needed * excess,
        "global_initialization_sei_once": global_sei,
        "container_keyframe_count": keyframes,
        "predicted_container_bytes": container,
        "frozen_v2_default_container_bytes": v2_container,
        "frozen_v2_default_file_estimate": max(1, round(raw + v2_container)),
        "corrected_base_payload_estimate": de_i_base,
        "interval_status": "uncalibrated; no confidence interval or coverage claim",
        "cost_status": "costed artifact replay; not a fresh deadline benchmark",
    }
    return [
        {
            **shared,
            "method": method,
            "predicted_payload_bytes": payload,
            "predicted_file_bytes": max(1, round(payload + container)),
        }
        for method, payload in zip(METHODS, payloads)
    ]


def matched_window_diagnostics(output, references, cases):
    """Scoring-only packet comparison; never an input to candidate predictions."""
    rows = []
    for case in cases:
        full = references[case["id"]]
        time_base = Fraction(full["time_base"])
        for path in sorted((output / "measurements").glob(f"{case['id']}-*.json")):
            probe = json.loads(path.read_text())
            start, duration = probe["window"]
            central = [
                packet
                for packet in full["packets"]
                if start <= float(packet["pts"] * time_base) < start + duration
            ]
            by_type = {}
            for kind in ("I", "P", "B"):
                sizes = [p["bytes"] for p in central if p["picture_type"] == kind]
                by_type[kind] = {
                    "count": len(sizes),
                    "payload_bytes": sum(sizes),
                    "mean_bytes": statistics.fmean(sizes) if sizes else None,
                }
            nonkeys = [p["bytes"] for p in central if not p["keyframe"]]
            rows.append(
                {
                    "case": case["id"],
                    "window": probe["window"],
                    "scoring_only": True,
                    "phase": "assessment_diagnostic",
                    "independent_probe_by_type": probe["by_picture_type"],
                    "same_source_window_in_continuous_by_type": by_type,
                    "independent_probe_nonkey_mean": probe["nonkey_mean_bytes"],
                    "continuous_window_nonkey_mean": statistics.fmean(nonkeys) if nonkeys else None,
                    "independent_probe_cleaned_payload": probe["deSEI_startupI_payload_bytes"],
                    "continuous_window_payload": sum(p["bytes"] for p in central),
                    "continuous_window_keyframe_count": sum(p["keyframe"] for p in central),
                }
            )
    write_json(output / "matched-window-diagnostics.json", rows)


def verify_parser(output, cases):
    """Small real decode cross-check, charged only to study verification."""
    checks = []
    for case in cases:
        if case["id"] not in ("static", "screen", "pedestrians"):
            continue
        path = next(iter(sorted((output / "measurements").glob(f"{case['id']}-*.json"))))
        record = json.loads(path.read_text())
        command = [
            "ffprobe",
            "-v",
            "error",
            "-threads",
            "1",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pict_type",
            "-of",
            "json",
            record["artifact"],
        ]
        frames = json.loads(subprocess.check_output(command))["frames"]
        packets = sorted(record["packets"], key=lambda p: p["pts"])
        if [p["picture_type"] for p in packets] != [f["pict_type"] for f in frames]:
            raise ValueError("AVCC slice types disagree with actual decoded frames")
        checks.append(
            {
                "case": case["id"],
                "frames": len(frames),
                "avcc_slice_picture_types_match_ffprobe_decode": True,
                "scoring_only_validation_command": command,
            }
        )
    assert sei_messages(b"\x06\x05\x10" + b"A" * 16 + b"\x80")[0]["payload_type"] == 5
    assert sei_messages(b"\x06\x04\x01Z\x05\x10" + b"A" * 16 + b"\x80")[0]["payload_type"] == 4
    assert sei_messages(b"\x06\x05\xff\x05" + b"A" * 260 + b"\x80")[0]["payload_bytes"] == 260
    assert rbsp(b"\x00\x00\x03\x01") == b"\x00\x00\x01"
    assert rbsp(b"\x00\x00\x03\x04") == b"\x00\x00\x03\x04"
    for nal in (b"\x01", b"\x01\x00"):
        try:
            slice_picture_type(nal)
        except ValueError:
            pass
        else:
            raise ValueError("truncated slice header was accepted")
    write_json(
        output / "parser-verification.json",
        {
            "real_ffprobe_crosschecks": checks,
            "synthetic_parser_checks": "passed",
            "scoring_only": True,
        },
    )


def study(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(MANIFEST.read_text())["cases"]
    if args.cases:
        requested = set(args.cases.split(","))
        if not requested <= {case["id"] for case in cases}:
            raise ValueError("only original development cases are allowed")
        cases = [case for case in cases if case["id"] in requested]
    metadata = {
        "split": "original-development-nine-only",
        "case_ids": [case["id"] for case in cases],
        "formula": FORMULA,
        "formula_fingerprint": canonical_key(FORMULA),
        "pipeline": PIPELINE_VERSION,
        "threads": 1,
        "preset": "medium",
        "crf": 23,
        "audio": "dropped; these development sources have no audio",
        "reference_access": "prediction code cannot access references; scores computed after all predictions are written",
        "ordered_phases": [
            "fixed_policy",
            "source_and_probe_measurement",
            "persist_all_predictions",
            "read_development_references",
            "assessment_and_diagnostics",
        ],
        "label_separation_scope": "algorithmic inference only; familiar development set, not a blinded evaluation",
        "study_kind": "development candidate; costed artifact replay",
        "v2_artifact_reuse_enabled": not args.no_v2_reuse,
    }
    phase_log = {"timing_basis": "actual UTC phase events for this invocation", "events": []}

    def phase(name, **extra):
        phase_log["events"].append(
            {
                "phase": name,
                "utc": datetime.now(timezone.utc).isoformat(),
                **extra,
            }
        )
        write_json(output / "phase-log.json", phase_log)

    # Persist the exact candidate policy before any measurement or scoring.
    write_json(output / "metadata.json", metadata)
    phase("fixed_policy", formula_fingerprint=metadata["formula_fingerprint"])
    phase("source_and_probe_measurement")
    predictions = []
    for case in cases:
        setup_start = time.monotonic()
        source = Path(case["source"])
        if digest(source) != case["sha256"]:
            raise ValueError("development source changed")
        deadline = RecordingDeadline(1200)
        media = Media(
            EncodeSpec(source, drop_audio=True, segment_seconds=1.0),
            1,
            Cache(output / ".vsize-cache" / case["id"]),
            deadline,
        )
        if media.audio is not None:
            raise ValueError("this narrow candidate study only handles video-only MP4")
        blocks = media.metadata_blocks(1.0)
        setup = time.monotonic() - setup_start
        featured, discovery = media.preview_features(blocks, 4)
        feature_path = output / "features" / f"{case['id']}.json"
        discovery["cold_elapsed_seconds"] = discovery["elapsed_seconds"]
        if discovery["cache_hit"] and feature_path.exists():
            previous = json.loads(feature_path.read_text())
            if previous["discovery"]["key"] == discovery["key"]:
                discovery["cold_elapsed_seconds"] = previous["discovery"]["cold_elapsed_seconds"]
        write_json(
            feature_path,
            {"source_hash": media.source_hash, "blocks": featured, "discovery": discovery},
        )
        plans = []
        required = set()
        for fraction in FRACTIONS:
            nominal = media.duration / len(blocks)
            pilots = min(3, len(blocks), 6, max(1, int(fraction * media.duration / nominal)))
            sampler = Sampler(copy.deepcopy(featured), SEED, count=pilots)
            indices = []
            for index in sampler.order[:6]:
                if (
                    sum(blocks[i]["duration"] for i in indices + [index])
                    > fraction * media.duration + 1e-7
                ):
                    break
                indices.append(index)
            plans.append((fraction, pilots, sampler, indices))
            required.update(indices)
        measured = {}
        for index in sorted(required):
            block = blocks[index]
            window = (block["start"], block["duration"])
            lookup_started = time.monotonic()
            record = None if args.no_v2_reuse else existing_probe(media, case["id"], window)
            lookup_wall = time.monotonic() - lookup_started
            reused = record is not None
            if record is None:
                record = media.encode(23.0, window)
            record = diagnose(record, deadline)
            record["artifact_reused_from_v2"] = reused
            record["cache_lookup_wall_seconds"] = lookup_wall
            record["block_index"] = index
            record["formula_fingerprint"] = metadata["formula_fingerprint"]
            measured[index] = record
            write_json(output / "measurements" / f"{case['id']}-{index:03}.json", record)
        for fraction, pilots, sampler, indices in plans:
            for count in range(1, len(indices) + 1):
                rows = predict(
                    sampler, measured, indices[:count], media, setup, discovery, fraction, pilots
                )
                predictions.extend({"case": case["id"], **row} for row in rows)
        media.verify_source()
        write_json(output / "commands" / f"{case['id']}.json", deadline.commands)
        write_json(output / "predictions.json", predictions)
        print(
            json.dumps(
                {
                    "case": case["id"],
                    "measured_indices": sorted(required),
                    "reused_probes": sum(r["artifact_reused_from_v2"] for r in measured.values()),
                    "startup_excess_range": [
                        min(r["startup_i_excess_bytes"] for r in measured.values()),
                        max(r["startup_i_excess_bytes"] for r in measured.values()),
                    ],
                }
            ),
            flush=True,
        )
    # References are used only for assessment after all predictions exist.
    phase(
        "persist_all_predictions",
        prediction_records=len(predictions),
        predictions_sha256=digest(output / "predictions.json"),
    )
    phase("read_development_references")
    references = json.loads(REFERENCES.read_text())
    evaluated = []
    reference_diagnostics = {}
    for case in cases:
        reference = references[case["id"]]
        if reference["source_hash"] != case["sha256"]:
            raise ValueError("reference source mismatch")
        if digest(Path(reference["artifact"])) != reference["sha256"]:
            raise ValueError("reference artifact changed")
        reference_diagnostics[case["id"]] = diagnose(reference, Deadline(120))
        reference_diagnostics[case["id"]]["scoring_only"] = True
        reference_diagnostics[case["id"]]["phase"] = "assessment_diagnostic"
    write_json(output / "reference-diagnostics.json", reference_diagnostics)
    matched_window_diagnostics(output, reference_diagnostics, cases)
    verify_parser(output, cases)
    for row in predictions:
        reference = references[row["case"]]
        evaluated.append(
            {
                **row,
                "phase": "assessment",
                "prediction_phase": "persisted_before_reference_read",
                "reference_data_used_for": "scoring_only",
                "reference_payload_bytes": reference["payload_bytes"],
                "reference_file_bytes": reference["file_bytes"],
                "reference_cold_encode_wall_seconds": reference["encode_wall_seconds"],
                "payload_relative_error": row["predicted_payload_bytes"]
                / reference["payload_bytes"]
                - 1,
                "file_relative_error": row["predicted_file_bytes"] / reference["file_bytes"] - 1,
                "costed_vs_full_encode_wall_ratio": row["costed_total_wall_seconds"]
                / reference["encode_wall_seconds"],
            }
        )
    write_json(output / "evaluated.json", evaluated)
    final = []
    for case in cases:
        for fraction in FRACTIONS:
            subset = [
                r for r in evaluated if r["case"] == case["id"] and r["budget_fraction"] == fraction
            ]
            maximum = max(r["sample_count"] for r in subset)
            final.extend(r for r in subset if r["sample_count"] == maximum)
    write_json(output / "final-prefixes.json", final)
    summary = []
    for fraction in FRACTIONS:
        for method in METHODS:
            subset = [
                r for r in final if r["budget_fraction"] == fraction and r["method"] == method
            ]
            summary.append(
                {
                    "budget_fraction": fraction,
                    "method": method,
                    "cases": len(subset),
                    "file_median_absolute_relative_error": statistics.median(
                        abs(r["file_relative_error"]) for r in subset
                    ),
                    "file_max_absolute_relative_error": max(
                        abs(r["file_relative_error"]) for r in subset
                    ),
                    "payload_median_absolute_relative_error": statistics.median(
                        abs(r["payload_relative_error"]) for r in subset
                    ),
                    "payload_max_absolute_relative_error": max(
                        abs(r["payload_relative_error"]) for r in subset
                    ),
                    "costed_median_wall_seconds": statistics.median(
                        r["costed_total_wall_seconds"] for r in subset
                    ),
                    "costed_median_vs_full_encode_ratio": statistics.median(
                        r["costed_vs_full_encode_wall_ratio"] for r in subset
                    ),
                    "within_replayed_5s_count": sum(
                        r["within_replayed_5s_wall_budget"] for r in subset
                    ),
                }
            )
    write_json(output / "summary.json", {"metadata": metadata, "final_prefix_summaries": summary})
    phase("assessment_and_diagnostics", evaluated_records=len(evaluated))
    columns = [
        "case",
        "budget_fraction",
        "method",
        "sample_count",
        "observed_indices",
        "predicted_file_bytes",
        "reference_file_bytes",
        "file_relative_error",
        "payload_relative_error",
        "startup_i_excess_observed_mean",
        "estimated_nonstartup_keyframe_count",
        "periodic_i_excess_bytes",
        "predicted_container_bytes",
        "reserved_encode_seconds",
        "costed_total_wall_seconds",
        "costed_vs_full_encode_wall_ratio",
        "reused_probe_count",
    ]
    with (output / "comparison.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(final)
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results/startup-study-2026-10-03")
    parser.add_argument("--cases")
    parser.add_argument(
        "--no-v2-reuse",
        action="store_true",
        help="collect probes outside the existing v2 cache; use a new output directory for fresh artifacts",
    )
    study(parser.parse_args())


if __name__ == "__main__":
    main()
