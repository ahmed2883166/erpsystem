import unittest

from industry_packs import analyze_activity_pack_compatibility


class IndustryPackCompatibilityTests(unittest.TestCase):
    def test_single_activity_template_has_no_cross_activity_conflicts(self):
        report = analyze_activity_pack_compatibility(["commercial"])
        self.assertEqual(report["activities"], ["commercial"])
        self.assertTrue(report["compatible"])
        self.assertEqual(report["chart_conflict_count"], 0)
        self.assertEqual(report["role_conflict_count"], 0)

    def test_commercial_and_industrial_exposes_receivables_inventory_collision(self):
        report = analyze_activity_pack_compatibility(["commercial", "industrial"])
        self.assertFalse(report["compatible"])
        collision = next(
            item for item in report["role_conflicts"]
            if item["account_code"] == "1130"
        )
        self.assertEqual(
            set(collision["role_families"]),
            {"receivables", "inventory"},
        )
        self.assertTrue(report["requires_account_mapping"])

    def test_shared_inventory_role_is_not_mistaken_for_role_conflict(self):
        report = analyze_activity_pack_compatibility(["medical", "restaurant"])
        self.assertFalse(report["compatible"])
        inventory_1150 = next(
            item for item in report["role_conflicts"]
            if item["account_code"] == "1150"
        ) if any(item["account_code"] == "1150" for item in report["role_conflicts"]) else None
        self.assertIsNone(inventory_1150)
        self.assertTrue(any(
            item["account_code"] == "1150"
            for item in report["chart_conflicts"]
        ))

    def test_structural_group_detail_collision_is_reported(self):
        report = analyze_activity_pack_compatibility(["commercial", "service"])
        collision = next(
            item for item in report["chart_conflicts"]
            if item["account_code"] == "1210"
        )
        self.assertEqual(
            {row["is_group"] for row in collision["definitions"]},
            {False, True},
        )

    def test_unknown_activity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "غير مدعومة"):
            analyze_activity_pack_compatibility(["commercial", "manufacturing"])


if __name__ == "__main__":
    unittest.main()
