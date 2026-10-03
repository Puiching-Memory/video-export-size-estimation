import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from vsize.trajectory import (
    ASSUMPTION,
    TrajectoryCalibration,
    family_key,
    generate_profile,
    harmonic_point,
    make_family,
    matches_family,
)


def family(**changes):
    arguments = {
        "configuration": {
            "codec": "libx264",
            "container": "mp4",
            "crf": 23.0,
            "preset": "medium",
            "video_filter": None,
            "audio_bitrate": 128000,
            "drop_audio": False,
        },
        "crf_grid": [23.0],
        "sample_seconds": 4.0,
        "probe_policy": {"kind": "independent", "padding_seconds": 0},
        "seed_strategy": {"kind": "fixed", "seed": 0},
        "toolchain_key": "test-build",
        "threads": 2,
        "prefix_policy": {
            "kind": "all_prefixes_up_to_maximum",
            "minimum_prefix": 1,
            "maximum_prefix": 3,
        },
    }
    arguments.update(changes)
    return make_family(**arguments)


def rows_and_provenance(registered_family, count, prefixes=(1,), variants=None):
    rows, provenance = [], {}
    variants = variants or registered_family["variants"]
    for index in range(count):
        group, source = f"group-{index}", f"source-{index}"
        provenance[group] = {
            "grouping_basis": "Test-only original source family, not business evidence",
            "source_ids": [source],
            "split": "calibration_reserved",
        }
        for variant in variants:
            for crf in registered_family["crf_grid"]:
                for prefix in prefixes:
                    # Artificial labels exercise the theorem and implementation.
                    # They do not establish any business-domain coverage.
                    rows.append(
                        {
                            "group_id": group,
                            "source_id": source,
                            "split": "calibration_reserved",
                            "variant_id": variant,
                            "crf": crf,
                            "prefix": prefix,
                            "source_blocks": max(prefixes),
                            "expected_prefixes": list(prefixes),
                            "predicted_bytes": 1000 * math.exp((index + 1) / 10),
                            "actual_bytes": 1000,
                        }
                    )
    return rows, provenance


class TrajectoryContracts(unittest.TestCase):
    def load(self, profiles):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "trajectory.json"
        path.write_text(json.dumps({"schema_version": 2, "profiles": profiles}))
        return TrajectoryCalibration(path)

    def profile(self, count=19, registered_family=None, prefixes=(1,)):
        registered_family = registered_family or family()
        rows, provenance = rows_and_provenance(registered_family, count, prefixes)
        return generate_profile(
            rows,
            "test-only artificial theorem check",
            registered_family,
            group_provenance=provenance,
        )

    def test_seven_groups_cannot_produce_a_95_percent_interval(self):
        profile = self.profile(7)
        calibration = self.load([profile])
        self.assertIsNone(calibration.interval(profile["family_key"], 1000, 0.95, sample_count=1))
        self.assertIn("do not establish independence", profile["assumption"])
        self.assertEqual(profile["independence_status"], "provenance_declared_not_verified")

    def test_nineteen_groups_use_the_maximum_score_at_95_percent(self):
        profile = self.profile(19)
        calibration = self.load([profile])
        result = calibration.interval(profile["family_key"], 1000, 0.95, sample_count=1)
        self.assertEqual(result["conformal_rank"], 19)
        self.assertEqual(result["calibration_groups"], 19)
        self.assertEqual(
            result["absolute_log_error_radius"],
            max(group["absolute_log_error"] for group in profile["groups"]),
        )
        self.assertIsNone(calibration.interval(profile["family_key"], 1000, 0.951, sample_count=1))
        self.assertEqual(result["coverage"], 0.95)

    def test_conformal_rank_is_ceil_g_plus_one_times_requested_coverage(self):
        profile = self.profile(24)
        result = self.load([profile]).interval(profile["family_key"], 1000, 0.9, sample_count=1)
        self.assertEqual(result["conformal_rank"], 23)
        scores = sorted(group["absolute_log_error"] for group in profile["groups"])
        self.assertEqual(result["absolute_log_error_radius"], scores[22])

    def test_family_hash_covers_all_parameters_and_current_candidate_membership(self):
        registered = family(crf_grid=[23, 26])
        configuration = {**registered["configuration"], "crf": 26}
        self.assertTrue(matches_family(registered, configuration))
        self.assertEqual(
            family_key(registered),
            family_key(family(configuration=configuration, crf_grid=[26, 23])),
        )
        changes = [
            {"sample_seconds": 2},
            {"probe_policy": {"kind": "independent", "padding_seconds": 1}},
            {"seed_strategy": {"kind": "fixed", "seed": 1}},
            {"toolchain_key": "different-build"},
            {"threads": 4},
            {"crf_grid": [23, 27]},
            {"variants": ("original", "scaled")},
            {"model_policy": "different-model"},
            {
                "prefix_policy": {
                    "kind": "all_prefixes_up_to_maximum",
                    "minimum_prefix": 2,
                    "maximum_prefix": 3,
                }
            },
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(family_key(registered), family_key(family(**change)))
        self.assertFalse(matches_family(registered, {**configuration, "crf": 28}))
        self.assertFalse(matches_family(registered, {**configuration, "preset": "fast"}))
        self.assertFalse(matches_family(registered, {"crf": 26}))
        with self.assertRaises(ValueError):
            family(configuration={**configuration, "crf": 28}, crf_grid=[23, 26])

    def test_unregistered_build_policy_grid_and_runtime_configuration_are_rejected(self):
        registered = family(crf_grid=[23, 26])
        profile = self.profile(19, registered)
        calibration = self.load([profile])
        for changed in [
            family(crf_grid=[23, 26], toolchain_key="other-build"),
            family(crf_grid=[23, 26], probe_policy={"kind": "context", "padding_seconds": 2}),
            family(crf_grid=[23, 28]),
        ]:
            with self.subTest(changed=changed):
                self.assertIsNone(
                    calibration.interval(family_key(changed), 1000, 0.95, sample_count=1)
                )
        candidate = {**registered["configuration"], "crf": 26}
        self.assertIsNotNone(
            calibration.interval(
                profile["family_key"], 1000, 0.95, sample_count=1, configuration=candidate
            )
        )
        for changed in [{**candidate, "crf": 28}, {**candidate, "drop_audio": True}]:
            self.assertIsNone(
                calibration.interval(
                    profile["family_key"], 1000, 0.95, sample_count=1, configuration=changed
                )
            )

    def test_group_max_does_not_count_prefixes_crfs_or_variants_as_sources(self):
        registered = family(crf_grid=[23, 26], variants=("original", "scaled"))
        rows, provenance = rows_and_provenance(registered, 7, prefixes=(1, 2, 3))
        rows[4]["predicted_bytes"] = 100000
        profile = generate_profile(rows, "test-only", registered, group_provenance=provenance)
        self.assertEqual(len(profile["groups"]), 7)
        first = profile["groups"][0]
        self.assertEqual(first["row_count"], 12)
        self.assertEqual(first["trajectory_count"], 4)
        self.assertAlmostEqual(first["absolute_log_error"], math.log(100))
        self.assertIsNone(
            self.load([profile]).interval(profile["family_key"], 1000, 0.95, sample_count=1)
        )

    def test_missing_or_duplicate_registered_prefixes_and_candidates_are_rejected(self):
        registered = family(crf_grid=[23, 26])
        rows, provenance = rows_and_provenance(registered, 1, prefixes=(1, 2, 3))
        mutations = [rows[:-1], rows + [rows[0]], rows[:3]]
        shortened = copy.deepcopy(rows)
        shortened[0]["expected_prefixes"] = [1, 3]
        mutations.append(shortened)
        changed_reference = copy.deepcopy(rows)
        changed_reference[1]["actual_bytes"] = 1100
        mutations.append(changed_reference)
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                generate_profile(changed, "test-only", registered, group_provenance=provenance)

    def test_test_split_and_undocumented_or_duplicated_source_groups_are_rejected(self):
        registered = family()
        rows, provenance = rows_and_provenance(registered, 2)
        for split in ["test", "test_reserved", "development"]:
            changed = copy.deepcopy(rows)
            changed[0]["split"] = split
            with self.subTest(split=split), self.assertRaises(ValueError):
                generate_profile(changed, "test-only", registered, group_provenance=provenance)
        with self.assertRaises(ValueError):
            generate_profile(rows, "test-only", registered, group_provenance={})
        changed = copy.deepcopy(rows)
        changed[1]["source_id"] = changed[0]["source_id"]
        with self.assertRaises(ValueError):
            generate_profile(changed, "test-only", registered, group_provenance=provenance)
        changed_provenance = copy.deepcopy(provenance)
        changed_provenance["group-0"]["split"] = "test_reserved"
        with self.assertRaises(ValueError):
            generate_profile(rows, "test-only", registered, group_provenance=changed_provenance)

    def test_bundle_cannot_silently_change_family_assumptions_or_duplicate_groups(self):
        profile = self.profile()
        changed_profiles = []
        changed = copy.deepcopy(profile)
        changed["family"]["threads"] = 4
        changed_profiles.append(changed)
        changed = copy.deepcopy(profile)
        changed["groups"][1]["group_id"] = changed["groups"][0]["group_id"]
        changed_profiles.append(changed)
        changed = copy.deepcopy(profile)
        changed["assumption"] = "unique identifiers prove independence"
        changed_profiles.append(changed)
        changed = copy.deepcopy(profile)
        changed["groups"][0]["splits"] = ["test_reserved"]
        changed_profiles.append(changed)
        for changed in changed_profiles:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.load([changed])
        with self.assertRaises(ValueError):
            self.load([profile, profile])
        self.assertEqual(profile["assumption"], ASSUMPTION)

    def test_leave_one_group_out_joint_coverage_protects_every_stop_or_selection(self):
        # Enumerate all possible held-out groups of an exchangeable 20-group
        # finite pool. The 19-group 95% rule can miss the unique largest group
        # trajectory. Every other group must cover every candidate/prefix;
        # any data-dependent choice from those points is consequently covered.
        # This is a theorem check on artificial scores, not a business result.
        registered = family(crf_grid=[23, 26], variants=("original", "scaled"))
        rows, provenance = rows_and_provenance(registered, 20, prefixes=(1, 2, 3))
        for row in rows:
            index = int(row["group_id"].split("-")[-1])
            weight = 1.0 if row["prefix"] == 3 and row["crf"] == 26 else 0.4
            sign = -1 if row["variant_id"] == "scaled" else 1
            row["predicted_bytes"] = 1000 * math.exp(sign * weight * (index + 1) / 10)
        joint_misses = 0
        for held in range(20):
            held_group = f"group-{held}"
            profile = generate_profile(
                [row for row in rows if row["group_id"] != held_group],
                "test-only",
                registered,
                group_provenance=provenance,
            )
            calibration = self.load([profile])
            misses = []
            for row in rows:
                if row["group_id"] != held_group:
                    continue
                result = calibration.interval(
                    profile["family_key"],
                    row["predicted_bytes"],
                    0.95,
                    sample_count=row["prefix"],
                )
                low, high = result["interval_bytes"]
                misses.append(not low <= row["actual_bytes"] <= high)
            joint_misses += any(misses)
        self.assertEqual(joint_misses, 1)
        self.assertLessEqual(joint_misses / 20, 0.05)

    def test_harmonic_choice_minimizes_relative_error_without_changing_model_point(self):
        self.assertEqual(harmonic_point([80, 120]), 96)
        self.assertEqual(harmonic_point([100, 200]), 133)
        for low, high in [(1, 2), (8, 35), (80, 120), (100, 200), (17, 17)]:
            point = harmonic_point([low, high])

            def error(candidate):
                return max(candidate / low - 1, 1 - candidate / high)

            self.assertAlmostEqual(
                error(point), min(error(candidate) for candidate in range(low, high + 1))
            )
        for interval in [[0, 2], [4, 3], [1.5, 3], [1], [float("nan"), 2]]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                harmonic_point(interval)

    def test_exact_large_byte_counts_are_not_lost_in_float_multiplication(self):
        registered = family()
        rows, provenance = rows_and_provenance(registered, 19)
        large = 2**53 + 1
        for row in rows:
            row["predicted_bytes"] = large
            row["actual_bytes"] = large
        profile = generate_profile(rows, "test-only", registered, group_provenance=provenance)
        result = self.load([profile]).interval(profile["family_key"], large, 0.95, sample_count=1)
        self.assertEqual(result["interval_bytes"], [large, large])

    def test_supplied_row_family_and_configuration_must_match_registration(self):
        registered = family()
        rows, provenance = rows_and_provenance(registered, 1)
        for extra in [
            {"family_key": "different-family"},
            {"configuration": {**registered["configuration"], "crf": 26}},
            {"configuration": {**registered["configuration"], "crf": 23, "preset": "fast"}},
        ]:
            changed = [{**rows[0], **extra}]
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                generate_profile(changed, "test-only", registered, group_provenance=provenance)

    def test_sample_count_cannot_escape_the_frozen_prefix_range(self):
        registered = family()
        profile = self.profile(19, registered, prefixes=(1, 2, 3))
        calibration = self.load([profile])
        self.assertIsNone(calibration.interval(profile["family_key"], 1000, 0.95))
        for count in [None, 0, -1, 4, 50, True, 1.5]:
            with self.subTest(count=count):
                self.assertIsNone(
                    calibration.interval(profile["family_key"], 1000, 0.95, sample_count=count)
                )
        for count in [1, 2, 3]:
            result = calibration.interval(profile["family_key"], 1000, 0.95, sample_count=count)
            self.assertIsNotNone(result)
            self.assertEqual(result["sample_count"], count)
            self.assertEqual(result["prefix_policy"]["maximum_prefix"], 3)
        larger_family = family(
            prefix_policy={
                "kind": "all_prefixes_up_to_maximum",
                "minimum_prefix": 1,
                "maximum_prefix": 50,
            }
        )
        self.assertNotEqual(family_key(registered), family_key(larger_family))

    def test_source_plan_counts_prevent_stopping_point_from_shrinking_registration(self):
        registered = family()
        rows, provenance = rows_and_provenance(registered, 1)
        # One observed prefix cannot pretend to be a complete three-prefix
        # trajectory when the frozen source plan actually contains ten blocks.
        missing = [{**rows[0], "source_blocks": 10}]
        with self.assertRaises(ValueError):
            generate_profile(missing, "test-only", registered, group_provenance=provenance)
        missing_count = copy.deepcopy(rows)
        del missing_count[0]["source_blocks"]
        with self.assertRaises(ValueError):
            generate_profile(missing_count, "test-only", registered, group_provenance=provenance)
        # A genuinely short source has only one valid prefix, even though its
        # family registers up to three. Registration records retain that fact.
        profile = generate_profile(rows, "test-only", registered, group_provenance=provenance)
        self.assertEqual(profile["groups"][0]["prefix_registration"][0]["source_blocks"], 1)
        changed = copy.deepcopy(profile)
        changed["groups"][0]["prefix_registration"][0]["source_blocks"] = 10
        with self.assertRaises(ValueError):
            self.load([changed])

    def test_default_scope_is_one_prefix_and_zero_scope_issues_no_certificate(self):
        default_family = family(prefix_policy=None)
        self.assertEqual(default_family["prefix_policy"]["maximum_prefix"], 1)
        rows, provenance = rows_and_provenance(default_family, 1)
        no_prefix_family = family(
            prefix_policy={
                "kind": "all_prefixes_up_to_maximum",
                "minimum_prefix": 1,
                "maximum_prefix": 0,
            }
        )
        self.assertEqual(no_prefix_family["prefix_policy"]["maximum_prefix"], 0)
        with self.assertRaises(ValueError):
            generate_profile(rows, "test-only", no_prefix_family, group_provenance=provenance)
        for policy in [
            {"kind": "all_registered_prefixes", "minimum_prefix": 1},
            {"kind": "all_prefixes_up_to_maximum", "minimum_prefix": 1},
            {
                "kind": "all_prefixes_up_to_maximum",
                "minimum_prefix": 1,
                "maximum_prefix": float("inf"),
            },
        ]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                family(prefix_policy=policy)


if __name__ == "__main__":
    unittest.main()
