""""Full-schema SQLite-to-PostgreSQL rehearsal using disposable databases only.

The SQLite source is initialized by the real NEXUS application in a subprocess.
The PostgreSQL target is a unique temporary schema built from the generated
full NEXUS baseline. No existing company database is opened.
"""
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from postgres_baseline import install_postgres_baseline_file
from postgres_database import connect_postgres
from scripts.copy_sqlite_to_postgres import copy_database
from scripts.verify_sqlite_postgres_copy import verify_copy


POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class FullNEXUSSQLitePostgreSQLCopyRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.schema = "nexus_full_copy_" + uuid.uuid4().hex[:12]
        self.pg = connect_postgres(POSTGRES_DSN, autocommit=True)
        self.pg.execute(f'CREATE SCHEMA "{self.schema}"')
        self.temp_dir = tempfile.TemporaryDirectory(prefix="nexus-full-copy-rehearsal-")
        self.root = Path(self.temp_dir.name)
        self.sqlite_path = self.root / "fresh_nexus.sqlite"
        self.ddl_path = self.root / "nexus_full_schema.sql"
        self.review_path = self.root / "nexus_full_schema_review.json"

        # Run app initialization in a fresh process so imported app_paths state
        # from the unit-test runner cannot redirect this rehearsal elsewhere.
        subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "export_initialized_sqlite_schema.py"),
                "--database-output",
                str(self.sqlite_path),
                "--seed-representative-data",
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        subprocess.run(
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "build_postgres_baseline.py"),
                "--output-dir",
                str(self.root),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        generated_ddl = self.root / "nexus_full_schema.sql"
        generated_review = self.root / "nexus_full_schema_review.json"
        generated_ddl.replace(self.ddl_path)
        generated_review.replace(self.review_path)

        # Install through the production baseline installer so the immutable
        # installation marker and exact DDL fingerprint are present before copy.
        install_postgres_baseline_file(
            self.pg, self.ddl_path, schema=self.schema
        )

    def tearDown(self):
        try:
            self.pg.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
        finally:
            self.pg.close()
            self.temp_dir.cleanup()

    def test_full_application_schema_copies_and_verifies_without_skipped_controls(self):
        import json

        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        self.assertEqual(review["source"]["kind"], "fresh_initialized_application_schema")
        self.assertGreater(review["table_count"], 50)
        self.assertEqual(review["foreign_key_count"] > 0, True)

        result = copy_database(
            str(self.sqlite_path),
            POSTGRES_DSN,
            schema=self.schema,
            batch_size=250,
            apply=True,
        )
        self.assertTrue(result["committed"])
        self.assertTrue(result["source_read_only"])
        self.assertEqual(len(result["table_row_counts"]), review["table_count"])
        self.assertGreater(result["total_rows"], 0)
        self.assertEqual(
            result["total_rows"],
            sum(result["table_row_counts"].values()),
        )
        self.assertGreater(result["table_row_counts"].get("users", 0), 0)
        self.assertGreater(result["table_row_counts"].get("accounts", 0), 0)
        self.assertEqual(result["table_row_counts"].get("journal_entries"), 1)
        self.assertEqual(result["table_row_counts"].get("journal_lines"), 2)
        self.assertEqual(result["table_row_counts"].get("stock_products"), 1)
        self.assertEqual(result["table_row_counts"].get("stock_warehouses"), 1)
        self.assertEqual(result["table_row_counts"].get("stock_movements"), 1)
        self.assertEqual(result["table_row_counts"].get("stock_valuation_layers"), 1)

        report = verify_copy(str(self.sqlite_path), POSTGRES_DSN, schema=self.schema)
        self.assertTrue(report["source_integrity_ok"])
        self.assertTrue(report["source_foreign_keys_ok"])
        self.assertEqual(report["source_table_count"], review["table_count"])
        self.assertEqual(report["matched_table_count"], review["table_count"])
        self.assertEqual(report["issue_count"], 0, report["issues"])
        self.assertEqual(
            report["financial_and_inventory_controls_skipped"],
            [],
            report["financial_and_inventory_controls_skipped"],
        )
        self.assertIn("journal_lines.debit_total", report["financial_and_inventory_controls"])
        self.assertIn("journal_lines.credit_total", report["financial_and_inventory_controls"])
        self.assertIn("journal_lines.debit_by_entry", report["financial_and_inventory_controls"])
        self.assertIn("journal_lines.credit_by_entry", report["financial_and_inventory_controls"])
        self.assertIn("stock_valuation_layers.qty_in_total", report["financial_and_inventory_controls"])
        self.assertIn("stock_valuation_layers.qty_remaining_total", report["financial_and_inventory_controls"])
        self.assertIn("stock_valuation_layers.value_remaining_total", report["financial_and_inventory_controls"])
        self.assertIn("stock_valuation_layers.value_remaining_by_product_warehouse", report["financial_and_inventory_controls"])
        self.assertTrue(report["passed"], report)


if __name__ == "__main__":
    unittest.main()
