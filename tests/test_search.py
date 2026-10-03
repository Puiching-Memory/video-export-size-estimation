import unittest

from vsize.search import CRFSearch


class MeasuredCRFSearch(unittest.TestCase):
    def test_secant_refines_overshoot_toward_higher_quality(self):
        search = CRFSearch(23, 26, 800)
        search.observe(23, 1000)
        search.observe(25, 600, record={"artifact": "verified-25.mp4"})
        self.assertEqual(search.best_actual().crf, 25)
        self.assertEqual(search.propose(), 24)
        search.observe(24, 800, record={"artifact": "verified-24.mp4"})
        self.assertEqual(search.best_actual().record["artifact"], "verified-24.mp4")
        self.assertIsNone(search.propose())
        self.assertEqual(search.summary()["optimality"], "lowest_verified_feasible_on_grid")

    def test_first_pair_measures_slope_instead_of_assuming_fixed_slope(self):
        search = CRFSearch(23, 35, 800)
        self.assertEqual(search.propose(), 23)
        search.observe(23, 1000)
        self.assertEqual(search.propose(), 24)
        search.observe(24, 900)
        self.assertEqual(search.propose(), 25)
        search.observe(25, 810)
        self.assertEqual(search.propose(), 26)
        search.observe(26, 729)
        self.assertEqual(search.best_actual().crf, 26)
        self.assertIsNone(search.propose())

    def test_estimates_propose_work_but_never_certify_feasibility(self):
        search = CRFSearch(23, 26, 800)
        search.observe(23, 1000, actual=False)
        search.observe(25, 600, actual=False)
        self.assertEqual(search.propose(), 24)
        self.assertIsNone(search.best_actual())
        self.assertEqual(search.summary()["feasibility"], "no_verified_feasible")

    def test_estimate_cannot_overwrite_actual_evidence(self):
        search = CRFSearch(23, 26, 800)
        search.observe(23, 1000)
        search.observe(23, 100, actual=False)
        search.observe(25, 600)
        self.assertEqual(search.propose(), 24)
        self.assertEqual(search.best_actual().crf, 25)
        self.assertEqual(len(search.observations), 3)

    def test_nonmonotonic_feasible_island_is_not_pruned(self):
        search = CRFSearch(23, 28, 800)
        search.observe(23, 1100)
        search.observe(25, 950)
        search.observe(27, 780)
        self.assertEqual(search.propose(), 26)
        search.observe(26, 830)
        self.assertEqual(search.propose(), 24)
        search.observe(24, 700)
        self.assertEqual(search.best_actual().crf, 24)
        self.assertIn("actual_size_is_nonmonotonic", search.summary()["warnings"])
        self.assertEqual(search.summary()["optimality"], "lowest_verified_feasible_on_grid")

    def test_cancelled_refinement_retains_best_verified_artifact(self):
        search = CRFSearch(23, 28, 800)
        search.observe(23, 1000)
        record = {"artifact": "verified.mp4", "file_bytes": 600}
        search.observe(27, 600, record=record)
        record["artifact"] = "caller-mutated.mp4"
        next_crf = search.propose()
        search.mark_attempted(next_crf)
        self.assertEqual(search.best_actual().record["artifact"], "verified.mp4")
        self.assertEqual(search.summary()["optimality"], "best_verified")
        self.assertIn(next_crf, search.summary()["unverified_lower_crfs"])

    def test_same_crf_disagreement_retains_all_actual_records(self):
        search = CRFSearch(23, 25, 800)
        search.observe(24, 750, record={"artifact": "first.mp4"})
        search.observe(24, 850, record={"artifact": "second.mp4"})
        self.assertEqual(search.best_actual().record["artifact"], "first.mp4")
        self.assertEqual(len(search.observations), 2)
        self.assertIn("actual_size_changed_at_same_crf", search.summary()["warnings"])

    def test_no_monotonicity_proof_is_inferred_from_flat_or_increasing_sizes(self):
        search = CRFSearch(23, 27, 800)
        search.observe(23, 1000)
        search.observe(24, 1100)
        self.assertEqual(search.propose(), 25)
        self.assertIn("actual_size_is_nonmonotonic", search.summary()["warnings"])
        self.assertEqual(search.summary()["feasibility"], "no_verified_feasible")

    def test_grid_infeasibility_requires_every_setting_measured(self):
        search = CRFSearch(23, 25, 800)
        search.observe(23, 1000)
        search.observe(24, 900, actual=False)
        search.observe(25, 810)
        self.assertIsNone(search.propose())
        self.assertEqual(search.summary()["feasibility"], "no_verified_feasible")
        search.observe(24, 900)
        self.assertEqual(search.summary()["feasibility"], "verified_infeasible_on_grid")

    def test_fractional_boundaries_and_proposal_idempotency(self):
        search = CRFSearch(23.25, 25.5, 800)
        self.assertEqual(search.grid, (23.25, 24.0, 25.0, 25.5))
        self.assertEqual(search.propose(), 23.25)
        self.assertEqual(search.propose(), 23.25)
        search.mark_attempted(23.25)
        self.assertEqual(search.propose(), 24)

    def test_input_validation(self):
        for arguments in [(23, 22, 800), (23, 52, 800), (float("nan"), 30, 800), (23, 30, 0)]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                CRFSearch(*arguments)
        with self.assertRaises(ValueError):
            CRFSearch(23, 30, 800, allowed_crfs=[22])
        search = CRFSearch(23, 25, 800)
        for crf, size in [(22, 1000), (24, 0), (24, float("nan")), (24, 100.5)]:
            with self.subTest(crf=crf, size=size), self.assertRaises(ValueError):
                search.observe(crf, size)


if __name__ == "__main__":
    unittest.main()
