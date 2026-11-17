"""PostgreSQL coverage for VAT ledger reconciliation and activity-profile lookup."""
import os
import unittest
import uuid
from decimal import Decimal


@unittest.skipUnless(
    os.environ.get("NEXUS_TEST_POSTGRES_DSN"),
    "NEXUS_TEST_POSTGRES_DSN is not configured",
)
class VatLedgerPostgreSQLTests(unittest.TestCase):
    def test_input_and_output_vat_reconcile_against_posted_ledger(self):
        import psycopg
        from psycopg.rows import dict_row
        from tax_center import _tax_ledger_reconciliation

        schema = "nexus_vat_ledger_" + uuid.uuid4().hex[:12]
        conn = psycopg.connect(
            os.environ["NEXUS_TEST_POSTGRES_DSN"], row_factory=dict_row
        )
        try:
            try:
                with conn.transaction():
                    conn.execute(f'CREATE SCHEMA "{schema}"')
                    conn.execute(f'SET LOCAL search_path TO "{schema}"')
                    conn.execute(
                        "CREATE TABLE company_settings(setting_key TEXT,setting_value TEXT)"
                    )
                    conn.execute(
                        "INSERT INTO company_settings VALUES('activity','commercial')"
                    )
                    conn.execute(
                        "CREATE TABLE accounts(id BIGINT PRIMARY KEY,code TEXT,name_ar TEXT,account_type TEXT)"
                    )
                    conn.execute(
                        "CREATE TABLE journal_entries(id BIGINT PRIMARY KEY,entry_date TEXT,status TEXT)"
                    )
                    conn.execute(
                        "CREATE TABLE journal_lines(id BIGINT PRIMARY KEY,entry_id BIGINT,account_id BIGINT,debit TEXT,credit TEXT)"
                    )
                    conn.cursor().executemany(
                        "INSERT INTO accounts VALUES(%s,%s,%s,%s)",
                        [
                            (1, "2130", "ضريبة مخرجات", "liability"),
                            (2, "1165", "ضريبة مدخلات", "asset"),
                        ],
                    )
                    conn.execute(
                        "INSERT INTO journal_entries VALUES(1,'2026-11-05','posted')"
                    )
                    conn.execute(
                        "INSERT INTO journal_entries VALUES(2,'2026-11-07','posted')"
                    )
                    conn.cursor().executemany(
                        "INSERT INTO journal_lines VALUES(%s,%s,%s,%s,%s)",
                        [
                            (1, 1, 1, "0", "137.20"),
                            (2, 2, 2, "56.00", "0"),
                        ],
                    )
                    summary = {
                        "period_start": "2026-11-01",
                        "period_end": "2026-12-01",
                        "output_vat": Decimal("137.20"),
                        "purchase_tax_total": Decimal("56.00"),
                        "input_vat": Decimal("56.00"),
                    }
                    result = _tax_ledger_reconciliation(conn, summary)
                    self.assertTrue(result["available"])
                    self.assertEqual(result["output_ledger"], Decimal("137.20"))
                    self.assertEqual(result["input_ledger"], Decimal("56.00"))
                    self.assertEqual(result["output_difference"], Decimal("0.00"))
                    self.assertEqual(result["input_difference"], Decimal("0.00"))
                    self.assertEqual(result["issues"], [])
                    raise RuntimeError("rollback disposable schema")
            except RuntimeError as exc:
                self.assertEqual(str(exc), "rollback disposable schema")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
