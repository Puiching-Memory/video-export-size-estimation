"""Two explicit export targets sharing the same controller and compute budget."""

import time
from dataclasses import asdict, replace
from fractions import Fraction

from . import context_probe
from .fmp4 import Track, planned_accounting, write_fragmented_mp4
from .runtime import canonical_key
from .mux_model import estimate_video_container, inspect_video_container
from .segmented_media import SegmentedMedia


class ContinuousSession:
    def __init__(self, media, request, charge, result):
        self.media, self.request, self.charge, self.result = media, request, charge, result
        self.blocks = media.metadata_blocks(request.sample_seconds)
        self.duration = media.duration
        self.records = {}
        self.mode = None

    def lookup_full(self, crf):
        return self.media.lookup(crf)

    def choose_probe_mode(self, first_index, can_encode):
        if self.mode is not None:
            return
        if self.request.probe_mode == "bare":
            self.mode = "bare"
        else:
            plan = context_probe.context_window(self.media, self.window(first_index))
            # Freeze before any candidate is encoded. Prefer context only when
            # the requested pilot count can all fit, including padding.
            count = self.result["pilot_count"]
            future = plan["requested_future_seconds"]
            cost = count * min(self.duration, self.request.sample_seconds + 2 + future)
            self.mode = (
                "context" if self.request.probe_mode == "context" or can_encode(cost) else "bare"
            )

    def window(self, index):
        block = self.blocks[index]
        return block["start"], block["duration"]

    def probe_plan(self, index, crf):
        window = self.window(index)
        if self.mode == "context":
            plan = context_probe.context_window(self.media, window)
            window = plan["encoding_window"]
        return window[1], self.media.lookup(crf, window)

    def probe(self, index, crf, cached=None):
        if cached is None:
            window = self.window(index)
            if self.mode == "context":
                window = context_probe.context_window(self.media, window)["encoding_window"]
            self._charge_audio(window)
        if self.mode == "context":
            record = context_probe.probe(self.media, crf, self.window(index))
            record = {**record, "payload_bytes": record["periodic_adjusted_payload_bytes"]}
        else:
            record = cached or self.media.encode(crf, self.window(index))
        if self.media.audio is None:
            artifact = record.get("warmup_artifact") or record["artifact"]
            record = {
                **record,
                "mux_census": inspect_video_container(artifact, self.media.deadline),
            }
        self.records[index] = record
        return record

    def estimate(self, sampler):
        estimate = sampler.estimate()
        initializer = 0
        if self.mode == "context":
            initializer = max(
                r["global_init_sei_bytes"] + r["global_audio_priming_bytes"]
                for r in sampler.observed.values()
            )
        census = [r["mux_census"] for r in sampler.observed.values() if r.get("mux_census")]
        if census:
            fps, _ = context_probe.output_fps(self.media)
            frames = max(1, round(self.duration * fps))
            container = round(
                sum(estimate_video_container(c, frames) for c in census) / len(census)
            )
            estimate["estimated_bytes"] = max(
                1, round(estimate["estimated_payload_bytes"] + container + initializer)
            )
            estimate["fixed_bytes"] = container + initializer
            estimate["container_model"] = "probe_mp4_sample_table_extrapolation"
        else:
            estimate["estimated_bytes"] += initializer
            estimate["fixed_bytes"] += initializer
            estimate["container_model"] = "2048_plus_6_per_estimated_video_frame"
        estimate["export_mode"] = "continuous"
        estimate["probe_mode"] = self.mode
        estimate["bias_status"] = "continuous_encoder_state_is_approximated"
        return estimate

    def probe_policy(self):
        lookahead, b_delay = context_probe.PRESET_CONTEXT[self.media.spec.preset]
        return {
            "kind": context_probe.CONTEXT_PROBE_VERSION,
            "leading_seconds": context_probe.LEADING_SECONDS,
            "lookahead_frames": lookahead,
            "b_delay_frames": b_delay,
            "guard_frames": context_probe.TRAILING_GUARD_FRAMES,
            "fps_rule": "last_explicit_fps_filter_else_source_average",
            "payload_rule": "central_pts_exclude_init_sei_plus_periodic_i_candidate",
            "global_initialization": "maximum_observed_added_once",
            "container_model": "mp4_table_census_v1_else_2048_plus_6_per_frame",
            "requested_mode": self.request.probe_mode,
            "automatic_mode_rule": "freeze_before_encoding_if_all_padded_pilots_fit",
            "max_encode_fraction": self.request.compute.max_encode_fraction,
            "bare_fallback": "independent-short-encode-v2",
        }

    def remaining_seconds(self, crf):
        return self.duration

    def expected_complete_wall(self, sampler):
        records = list(sampler.observed.values())
        if not records:
            return float("inf")
        encoded_seconds = sum(
            r.get("requested_encoded_media_seconds", self.blocks[i]["duration"])
            for i, r in sampler.observed.items()
        )
        rate = sum(r.get("transcode_wall_seconds", r["encode_wall_seconds"]) for r in records)
        fixed = max(
            r["encode_wall_seconds"] - r.get("transcode_wall_seconds", r["encode_wall_seconds"])
            for r in records
        )
        return max(0.1, (fixed + rate / encoded_seconds * self.duration) * 1.5)

    def complete(self, crf):
        if cached := self.lookup_full(crf):
            return cached
        self.charge(self.duration)
        self._charge_audio(None)
        return self.media.encode(crf)

    def _charge_audio(self, window):
        if self.media.audio is None:
            return
        duration = float(self.media.audio.get("duration") or self.media.info["format"]["duration"])
        seconds = duration if window is None else min(window[1], max(0, duration - window[0]))
        self.result["attempted_audio_seconds"] += seconds


class SegmentedSession:
    def __init__(self, media, request, charge, result):
        self.media, self.request, self.charge = media, request, charge
        self.adapter = SegmentedMedia(media, media.spec.segment_seconds)
        self.plan = self.adapter.prepare()
        self.duration = self.adapter.duration
        self.blocks = [
            {
                "start": float(segment.start_frame / self.adapter.output_rate),
                "duration": float(segment.frame_count / self.adapter.output_rate),
                "input_bytes": 0,
            }
            for segment in self.adapter.segments
        ]
        self.result = result
        self.result["segment_plan"] = self.plan
        self.records = {}
        self.audio_record = None
        self.audio_track = None
        self.audio_packets = None
        self.audio_ready = media.audio is None

    def key(self, crf):
        return canonical_key(
            {
                "pipeline": "fixed-fragment-mp4-v1",
                "segment_pipeline": self.plan["pipeline"],
                "segment_plan": self.plan["plan_hash"],
                "source": self.media.source_hash,
                "toolchain": self.media.toolchain_key,
                "threads": self.media.threads,
                "configuration": replace(self.media.spec, crf=crf).configuration(),
            }
        )

    def lookup_full(self, crf):
        return self.media.cache.lookup(self.key(crf), self.media.deadline)

    def choose_probe_mode(self, first_index, can_encode):
        pass

    def probe_plan(self, index, crf):
        return self.blocks[index]["duration"], self.adapter.lookup_segment(index, crf)

    def probe(self, index, crf, cached=None):
        record = cached or self.adapter.encode_segment(index, crf)
        self.records[(crf, index)] = record
        self._prepare_audio()
        return record

    def _prepare_audio(self):
        if self.audio_ready:
            return
        started = time.monotonic()
        try:
            cached = self.adapter.lookup_audio()
            if cached is None:
                self.result["attempted_audio_seconds"] += float(
                    self.media.audio.get("duration") or self.media.info["format"]["duration"]
                )
            self.audio_record = cached or self.adapter.encode_audio()
            self.audio_track, self.audio_packets = self.adapter.read_audio(self.audio_record)
            self.audio_ready = True
        finally:
            self.result["audio_preparation_seconds"] += time.monotonic() - started

    @staticmethod
    def _track(record):
        return Track(
            codec="h264",
            time_base=Fraction(record["time_base"]),
            extradata=bytes.fromhex(record["extradata_hex"]),
            width=record["width"],
            height=record["height"],
        )

    def accounting(self, record):
        track = self._track(record)
        ticks = Fraction(self.adapter.total_frames, 1) / self.adapter.output_rate / track.time_base
        if ticks.denominator != 1:
            raise RuntimeError("segment plan duration cannot be represented in track ticks")
        return planned_accounting(
            track,
            self.adapter.total_frames,
            int(ticks),
            len(self.blocks),
            audio_track=self.audio_track,
            audio_packets=self.audio_packets,
        )

    def estimate(self, sampler):
        accounting = self.accounting(next(iter(sampler.observed.values())))
        estimate = sampler.estimate(fixed_bytes=accounting.total_bytes)
        estimate.update(
            export_mode="segmented",
            probe_mode="actual_export_segments",
            bias_status="sampled_segments_are_reused_unchanged",
            known_byte_accounting=asdict(accounting),
            reusable_sample_count=len(sampler.observed),
        )
        return estimate

    def probe_policy(self):
        return {
            "kind": "actual-independent-cfr-segments",
            "pipeline": self.plan["pipeline"],
            "segment_seconds": self.media.spec.segment_seconds,
            "container": "fixed-fragment-mp4-v1",
            "audio": "whole_first_aac_track_once",
        }

    def remaining_seconds(self, crf):
        return sum(
            block["duration"] for i, block in enumerate(self.blocks) if (crf, i) not in self.records
        )

    def expected_complete_wall(self, sampler):
        if not sampler.observed:
            return float("inf")
        seconds = sum(self.blocks[i]["duration"] for i in sampler.observed)
        rate = sum(r["encode_wall_seconds"] for r in sampler.observed.values()) / seconds
        observed_count = len(sampler.observed)
        remaining = sum(
            b["duration"] for i, b in enumerate(self.blocks) if i not in sampler.observed
        )
        # Include one ffprobe read per completed piece and the final mux/identity check.
        return 1.5 * rate * remaining + 0.1 * len(self.blocks) + 0.1 * observed_count + 0.25

    def complete(self, crf):
        if cached := self.lookup_full(crf):
            return cached
        started = time.monotonic()
        reused = 0
        records = []
        for i, block in enumerate(self.blocks):
            record = self.records.get((crf, i)) or self.adapter.lookup_segment(i, crf)
            if record is None:
                self.charge(block["duration"])
                record = self.adapter.encode_segment(i, crf)
            else:
                reused += 1
            self.records[(crf, i)] = record
            records.append(record)
        self._prepare_audio()
        track = None
        packets = []
        for record in records:
            current_track, current_packets = self.adapter.read_segment(record)
            if track is None:
                track = current_track
            packets.append(current_packets)
        temporary = self.media.cache.temporary()
        # The writer exclusively creates its output; this private reservation
        # belongs to this task and can be removed before that creation.
        temporary.unlink()
        try:
            accounting = write_fragmented_mp4(
                temporary,
                track,
                packets,
                audio_track=self.audio_track,
                audio_packets=self.audio_packets,
                deadline=self.media.deadline,
            )
            planned = self.accounting(records[0])
            expected_bytes = planned.total_bytes + sum(r["payload_bytes"] for r in records)
            if (
                accounting.total_bytes != expected_bytes
                or temporary.stat().st_size != expected_bytes
            ):
                raise RuntimeError("fragmented export violated exact byte accounting")
            self.adapter.verify_source()
            self.result["segment_plan"] = self.adapter.plan()
            key = self.key(crf)
            return self.media.cache.publish(
                key,
                temporary,
                {
                    "key": key,
                    "crf": crf,
                    "source_hash": self.media.source_hash,
                    "toolchain_key": self.media.toolchain_key,
                    "configuration": replace(self.media.spec, crf=crf).configuration(),
                    "encode_wall_seconds": time.monotonic() - started,
                    "duration": self.duration,
                    "video_frames": self.adapter.total_frames,
                    "byte_accounting": asdict(accounting),
                    "reused_segments": reused,
                    "segment_count": len(records),
                    "audio_duration_metadata": "may_include_one_AAC_priming_frame_in_ffmpeg",
                },
                self.media.deadline,
            )
        finally:
            temporary.unlink(missing_ok=True)
