import itertools
import json
import tempfile
import unittest
from pathlib import Path

from vsize import ComputeBudget, EncodeSpec, Reliability, Request, SizeConstraint
from vsize.calibration import Calibration, policy_key, relative_error_bound, risk_multiplier
from vsize.inference import Sampler


class Contracts(unittest.TestCase):
    def test_axes_validate_independently(self):
        for kwargs in [
            {"wall_seconds": 0},
            {"wall_seconds": float("nan")},
            {"max_probes": -1},
            {"max_encode_fraction": -1},
            {"threads": 0},
            {"threads": 1.5},
            {"max_probes": float("nan")},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ComputeBudget(**kwargs)
        for kwargs in [{"relative_error": 0}, {"coverage": 1}, {"coverage": float("nan")}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Reliability(**kwargs)
        with self.assertRaises(ValueError):
            SizeConstraint(max_bytes=0)
        with self.assertRaises(ValueError):
            SizeConstraint(max_bytes=float("nan"))
        with self.assertRaises(ValueError):
            Request(EncodeSpec(Path("x"), crf=30), size=SizeConstraint(max_bytes=100, max_crf=20))
        with self.assertRaises(ValueError):
            Request.from_dict({"encode": {"source": "x"}, "typo": 1})

    def test_filters_cannot_hide_external_inputs_or_time_dependence(self):
        for value in ["movie=secret.mp4", "drawtext=textfile=private.txt", "eq=brightness=sin(t)"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                EncodeSpec(Path("x"), video_filter=value)
        EncodeSpec(Path("x"), video_filter="scale=320:180,fps=24,eq=brightness=0.1:saturation=0.8")

    def test_calibration_needs_enough_independent_groups_for_all_looks(self):
        profile = {
            "policy_key": "test",
            "domain": "test-only",
            "groups": [
                {"group_id": str(i), "absolute_log_error": 0.01 * (i + 1)} for i in range(19)
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "calibration.json"
            path.write_text(json.dumps({"schema_version": 1, "profiles": [profile]}))
            calibration = Calibration(path)
            single = calibration.interval("test", 1000, 0.95, 1)
            self.assertIsNotNone(single)
            self.assertIsNone(calibration.interval("test", 1000, 0.95, 2))
            self.assertIsNone(calibration.interval("missing", 1000, 0.95, 1))
            profile["groups"][1]["group_id"] = "0"
            path.write_text(json.dumps({"schema_version": 1, "profiles": [profile]}))
            with self.assertRaises(ValueError):
                Calibration(path)

    def test_relative_error_is_relative_to_truth(self):
        self.assertAlmostEqual(relative_error_bound(100, [80, 120]), 0.25)

    def test_calibration_cannot_cross_encoder_builds_or_thread_settings(self):
        arguments = (EncodeSpec(Path("x")).configuration(), 2, 3, 0)
        first = policy_key(*arguments, "build-a", 2)
        self.assertNotEqual(first, policy_key(*arguments, "build-b", 2))
        self.assertNotEqual(first, policy_key(*arguments, "build-a", 4))

    def test_sequential_risk_spending_covers_all_prefixes_and_configurations(self):
        # The finite partial sum has the closed form 1 - 1/(n+1), independently
        # of how many allowed CRFs are later selected adaptively.
        for configurations in [1, 13]:
            total = configurations * sum(
                1 / risk_multiplier(j, configurations) for j in range(1, 501)
            )
            self.assertAlmostEqual(total, 1 - 1 / 501)
            self.assertLess(total, 1)

    def test_random_audit_corrects_proxy_bias_in_expectation(self):
        # Enumerate every audit sample, with independent ground-truth block sizes.
        # The arithmetic must recover their total, despite an intentionally poor proxy.
        blocks = [{"start": i, "duration": 1.0, "input_bytes": 100} for i in range(6)]
        actual = [90, 120, 700, 55, 300, 180]
        template = Sampler(blocks, 0, count=1)
        totals = []
        for audited in itertools.combinations(template.audit_order, 2):
            sampler = Sampler(blocks, 0, count=1)
            for i in sampler.pilots + list(audited):
                sampler.add(i, {"payload_bytes": actual[i], "video_frames": 24})
            totals.append(sampler.estimate()["estimated_bytes"] - 2048 - 6 * 24 * 6)
        self.assertAlmostEqual(sum(totals) / len(totals), sum(actual), delta=0.5)
        self.assertEqual(sampler.estimate()["uncertainty"]["kind"], "uncalibrated")


if __name__ == "__main__":
    unittest.main()
