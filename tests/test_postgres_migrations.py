"""Tests for the opt-in PostgreSQL migration runner.

Integration tests use unique disposable schemas only. They never touch the
application schema or a company database.
"""
import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from db_compat import _split_sql_statements
from postgres_database import connect_postgres
from postgres_migrations import Migration, apply_migrations, load_migrations, quote_schema_name

POSTGRES_DSN = os.environ.get("NEXUS_TEST_POSTGRES_DSN", "").strip()


class PostgreSQLMigrationUnitTests(unittest.TestCase):
    def test_schema_identifier_is_validated_and_quoted(self):
        self.assertEqual(quote_schema_name("nexus_test"), '"nexus_test"')
        for value in ("", "nexus.test", "nexus-test", 'nexus"; DROP SCHEMA public;--', "x" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quote_schema_name(value)

    def test_loads_migrations_in_version_order_with_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0002_add_second.sql").write_text(
                "CREATE TABLE second_table (id BIGINT);", encoding="utf-8"
            )
            (root / "0001_create_first.sql").write_text(
                "CREATE TABLE first_table (id BIGINT);", encoding="utf-8"
            )
            migrations = load_migrations(root)
        self.assertEqual([item.version for item in migrations], [1, 2])
        self.assertEqual(migrations[0].name, "0001_create_first.sql")
        self.assertEqual(len(migrations[0].checksum), 64)

    def test_repository_core_migration_is_loadable_and_splits_function_body_safely(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual([item.version for item in migrations], [1, 2, 3, 4, 5, 6, 7, 8, 9])
        statements = _split_sql_statements(migrations[0].sql)
        audit_function = next(
            item for item in statements
            if "CREATE OR REPLACE FUNCTION reject_audit_log_mutation" in item
        )
        self.assertIn("RAISE EXCEPTION 'Audit log records are immutable';", audit_function)
        self.assertIn("END;\n$nexus_audit$", audit_function)
        self.assertTrue(any("debit NUMERIC(24, 6)" in item for item in statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS customers" in item for item in _split_sql_statements(migrations[1].sql)))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS bank_statement_balances" in item for item in _split_sql_statements(migrations[1].sql)))
        inventory_statements = _split_sql_statements(migrations[2].sql)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS stock_products" in item for item in inventory_statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS stock_valuation_layers" in item for item in inventory_statements))
        self.assertTrue(any("NUMERIC(24, 8)" in item for item in inventory_statements))
        purchasing_statements = _split_sql_statements(migrations[3].sql)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS purchase_orders" in item for item in purchasing_statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS goods_receipts" in item for item in purchasing_statements))
        sales_statements = _split_sql_statements(migrations[4].sql)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS sales_invoices" in item for item in sales_statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS customer_receipts" in item for item in sales_statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS sales_credit_notes" in item for item in sales_statements))
        location_integrity_statements = _split_sql_statements(migrations[5].sql)
        self.assertTrue(any("CREATE OR REPLACE FUNCTION nexus_validate_active_product_location" in item
                            for item in location_integrity_statements))
        self.assertTrue(any("CREATE TRIGGER trg_stock_product_locations_active_insert" in item
                            for item in location_integrity_statements))
        self.assertTrue(any("CREATE TRIGGER trg_stock_product_locations_active_update" in item
                            for item in location_integrity_statements))
        stock_value_statements = _split_sql_statements(migrations[6].sql)
        self.assertTrue(any("CREATE OR REPLACE FUNCTION nexus_initialize_stock_layer_value" in item
                            for item in stock_value_statements))
        self.assertTrue(any("CREATE TRIGGER trg_stock_layer_initialize_value" in item
                            for item in stock_value_statements))
        movement_uom_statements = _split_sql_statements(migrations[7].sql)
        self.assertTrue(any("fk_stock_movements_entered_uom" in item and "NOT VALID" in item
                            for item in movement_uom_statements))
        supplier_return_statements = _split_sql_statements(migrations[8].sql)
        self.assertTrue(any("ADD COLUMN IF NOT EXISTS supplier_total" in item
                            for item in supplier_return_statements))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS supplier_credit_note_bill_line_allocations" in item
                            for item in supplier_return_statements))

    def test_rejects_invalid_or_empty_migration_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.txt").write_text("not a migration", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid PostgreSQL migration filename"):
                load_migrations(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0001_empty.sql").write_text("  ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot be empty"):
                load_migrations(root)

    def test_rejects_migration_checksum_mismatch_before_execution(self):
        class Connection:
            autocommit = False
            info = None

            def execute(self, *_args):
                raise AssertionError("No SQL should execute for an invalid migration.")

        sql = 'CREATE TABLE "probe" (id BIGINT)'
        migration = Migration(
            version=1,
            name="0001_probe.sql",
            sql=sql,
            checksum=hashlib.sha256(b"different SQL").hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "checksum does not match"):
            apply_migrations(Connection(), [migration], schema="nexus_test")

    def test_rejects_migration_name_version_mismatch_before_execution(self):
        class Connection:
            autocommit = False
            info = None

            def execute(self, *_args):
                raise AssertionError("No SQL should execute for an invalid migration.")

        sql = 'CREATE TABLE "probe" (id BIGINT)'
        migration = Migration(
            version=2,
            name="0001_probe.sql",
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "does not match version"):
            apply_migrations(Connection(), [migration], schema="nexus_test")

    def test_rejects_autocommit_connections_before_execution(self):
        class Connection:
            autocommit = True
            info = None

        with self.assertRaisesRegex(ValueError, "autocommit=False"):
            apply_migrations(Connection(), [], schema="nexus_test")


@unittest.skipUnless(POSTGRES_DSN, "NEXUS_TEST_POSTGRES_DSN is not configured")
class PostgreSQLMigrationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = connect_postgres(POSTGRES_DSN)
        self.schema = "nexus_mig_" + uuid.uuid4().hex[:12]

    def tearDown(self):
        if getattr(self, "connection", None):
            self.connection.rollback()
            self.connection.execute(
                "DROP SCHEMA IF EXISTS " + quote_schema_name(self.schema) + " CASCADE"
            )
            self.connection.commit()
            self.connection.close()

    @staticmethod
    def _migration(version, name, sql):
        return Migration(
            version=version,
            name=name,
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )

    def test_applies_once_and_is_idempotent_in_target_schema(self):
        migrations = [
            self._migration(1, "0001_create_probe.sql",
                            'CREATE TABLE "probe" (id BIGINT PRIMARY KEY)')
        ]
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1])
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [])
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM "
            + quote_schema_name(self.schema)
            + '."schema_migrations"'
        ).fetchone()
        self.assertEqual(row["count"], 1)
        table = self.connection.execute(
            "SELECT to_regclass(%s) AS table_name",
            (self.schema + ".probe",),
        ).fetchone()
        self.assertIsNotNone(table["table_name"])

    def test_executes_multi_statement_migration_with_dollar_quoted_block(self):
        migrations = [
            self._migration(
                1,
                "0001_multi_statement.sql",
                """
                CREATE TABLE "multi_probe" (id BIGINT PRIMARY KEY, label TEXT);
                INSERT INTO "multi_probe"(id, label) VALUES (1, 'first; value');
                DO $nexus$
                BEGIN
                    UPDATE "multi_probe" SET label = 'done; value' WHERE id = 1;
                END
                $nexus$;
                """,
            )
        ]
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1])
        row = self.connection.execute(
            'SELECT label FROM ' + quote_schema_name(self.schema) + '."multi_probe" WHERE id=1'
        ).fetchone()
        self.assertEqual(row["label"], "done; value")

    def test_rejects_migration_set_missing_previously_applied_versions(self):
        original = self._migration(1, "0001_probe.sql", 'CREATE TABLE "probe" (id BIGINT)')
        apply_migrations(self.connection, [original], schema=self.schema)
        with self.assertRaisesRegex(RuntimeError, "missing from the supplied migration set: 1"):
            apply_migrations(self.connection, [], schema=self.schema)

    def test_modified_applied_migration_is_rejected(self):
        original = self._migration(1, "0001_probe.sql", 'CREATE TABLE "probe" (id BIGINT)')
        apply_migrations(self.connection, [original], schema=self.schema)
        changed = self._migration(
            1, "0001_probe.sql", 'CREATE TABLE "probe" (id BIGINT, label TEXT)'
        )
        with self.assertRaisesRegex(RuntimeError, "was modified"):
            apply_migrations(self.connection, [changed], schema=self.schema)

    def test_native_core_schema_preserves_exact_accounting_amounts(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual(apply_migrations(self.connection, migrations[:1], schema=self.schema), [1])

        schema = quote_schema_name(self.schema)
        user_id = self.connection.execute(
            f"INSERT INTO {schema}.users(username,full_name,password_hash,role,created_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            ("pg_core_test", "PG Test", "hash", "admin", "2026-01-01"),
        ).fetchone()["id"]
        account_id = self.connection.execute(
            f"INSERT INTO {schema}.accounts(code,name_ar,account_type) "
            "VALUES (%s,%s,%s) RETURNING id",
            ("1000", "Cash", "asset"),
        ).fetchone()["id"]
        entry_id = self.connection.execute(
            f"INSERT INTO {schema}.journal_entries(entry_date,description,created_by,created_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            ("2026-01-01", "Precision probe", user_id, "2026-01-01"),
        ).fetchone()["id"]
        self.connection.execute(
            f"INSERT INTO {schema}.journal_lines(entry_id,account_id,debit,credit) "
            "VALUES (%s,%s,%s,%s)",
            (entry_id, account_id, "1234567890123.123456", "0"),
        )
        amount = self.connection.execute(
            f"SELECT debit FROM {schema}.journal_lines WHERE entry_id=%s",
            (entry_id,),
        ).fetchone()["debit"]
        self.assertEqual(str(amount), "1234567890123.123456")
        self.assertGreater(entry_id, 0)

        audit_id = self.connection.execute(
            f"INSERT INTO {schema}.audit_log(action,details,created_at) "
            "VALUES (%s,%s,%s) RETURNING id",
            ("test", "immutable", "2026-01-01"),
        ).fetchone()["id"]
        self.connection.commit()
        with self.assertRaises(Exception):
            self.connection.execute(
                f"UPDATE {schema}.audit_log SET details=%s WHERE id=%s",
                ("tampered", audit_id),
            )
        self.connection.rollback()
        with self.assertRaises(Exception):
            self.connection.execute(
                f"DELETE FROM {schema}.audit_log WHERE id=%s",
                (audit_id,),
            )
        self.connection.rollback()
        audit_count = self.connection.execute(
            f"SELECT COUNT(*) AS count FROM {schema}.audit_log WHERE id=%s",
            (audit_id,),
        ).fetchone()["count"]
        self.assertEqual(audit_count, 1)

        with self.assertRaises(Exception):
            self.connection.execute(
                f"INSERT INTO {schema}.journal_lines(entry_id,account_id,debit,credit) "
                "VALUES (%s,%s,%s,%s)",
                (entry_id, account_id, "1", "1"),
            )
        self.connection.rollback()

    def test_party_and_banking_schema_supports_independent_ledgers(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [])
        schema = quote_schema_name(self.schema)
        # Trigger functions intentionally resolve their tables through the
        # active tenant schema, as they do in a tenant-configured app connection.
        self.connection.execute("SET search_path TO " + schema)

        account_id = self.connection.execute(
            f"INSERT INTO {schema}.accounts(code,name_ar,account_type) VALUES (%s,%s,%s) RETURNING id",
            ("1100", "Main Bank", "asset"),
        ).fetchone()["id"]
        self.connection.execute(
            f"INSERT INTO {schema}.currencies(code,name_ar,symbol,active,is_base,rate_to_base) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            ("EGP", "Egyptian Pound", "EGP", 1, 1, "1"),
        )
        cash_id = self.connection.execute(
            f"INSERT INTO {schema}.cash_accounts(kind,name_ar,account_id,currency_code,opening_date) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            ("bank", "Main Bank", account_id, "EGP", "2026-01-01"),
        ).fetchone()["id"]
        self.assertGreater(cash_id, 0)

        customer_id = self.connection.execute(
            f"INSERT INTO {schema}.customers(code,name) VALUES (%s,%s) RETURNING id",
            ("C-0001", "Customer One"),
        ).fetchone()["id"]
        supplier_id = self.connection.execute(
            f"INSERT INTO {schema}.suppliers(code,name) VALUES (%s,%s) RETURNING id",
            ("S-0001", "Supplier One"),
        ).fetchone()["id"]
        self.assertGreater(customer_id, 0)
        self.assertGreater(supplier_id, 0)

        with self.assertRaises(Exception):
            self.connection.execute(
                f"INSERT INTO {schema}.bank_transactions(transaction_no,transaction_date,bank_account_id,"
                "transaction_type,amount,description) VALUES (%s,%s,%s,%s,%s,%s)",
                ("BAD-TRANSFER", "2026-01-01", account_id, "transfer", "10", "Missing destination"),
            )
        self.connection.rollback()

    def test_purchasing_schema_creates_order_receipt_and_bill_links(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [])
        schema = quote_schema_name(self.schema)
        user_id = self.connection.execute(
            f"INSERT INTO {schema}.users(username,full_name,password_hash,role,created_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            ("purchase_test", "Purchase Test", "hash", "admin", "2026-01-01"),
        ).fetchone()["id"]
        supplier_id = self.connection.execute(
            f"INSERT INTO {schema}.suppliers(code,name) VALUES (%s,%s) RETURNING id",
            ("SUP-PG-1", "Supplier PG"),
        ).fetchone()["id"]
        product_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_products(sku,name_ar) VALUES (%s,%s) RETURNING id",
            ("SKU-PG-1", "Test Product"),
        ).fetchone()["id"]
        warehouse_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_warehouses(code,name_ar) VALUES (%s,%s) RETURNING id",
            ("WH-PG-1", "Main Warehouse"),
        ).fetchone()["id"]
        po_id = self.connection.execute(
            f"INSERT INTO {schema}.purchase_orders(po_no,supplier_id,order_date,created_by) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            ("PO-PG-1", supplier_id, "2026-01-02", user_id),
        ).fetchone()["id"]
        po_line_id = self.connection.execute(
            f"INSERT INTO {schema}.purchase_order_lines(po_id,product_id,quantity,unit_price) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (po_id, product_id, "12.5", "4.25"),
        ).fetchone()["id"]
        receipt_id = self.connection.execute(
            f"INSERT INTO {schema}.goods_receipts(receipt_no,po_id,supplier_id,warehouse_id,receipt_date) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            ("GR-PG-1", po_id, supplier_id, warehouse_id, "2026-01-03"),
        ).fetchone()["id"]
        receipt_line_id = self.connection.execute(
            f"INSERT INTO {schema}.goods_receipt_lines(receipt_id,po_line_id,product_id,quantity,unit_cost,total_cost) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (receipt_id, po_line_id, product_id, "12.5", "4.25", "53.125"),
        ).fetchone()["id"]
        bill_id = self.connection.execute(
            f"INSERT INTO {schema}.supplier_bills(bill_no,supplier_id,bill_date,supplier_invoice_no,subtotal,total_amount) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            ("BILL-PG-1", supplier_id, "2026-01-04", "INV-1", "53.125", "53.125"),
        ).fetchone()["id"]
        self.connection.execute(
            f"INSERT INTO {schema}.supplier_bill_receipts(bill_id,receipt_id,matched_amount) VALUES (%s,%s,%s)",
            (bill_id, receipt_id, "53.125"),
        )
        self.connection.execute(
            f"INSERT INTO {schema}.supplier_bill_inventory_lines(bill_id,line_no,receipt_line_id,product_id,"
            "billed_quantity,invoice_unit_price,line_total) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (bill_id, 1, receipt_line_id, product_id, "12.5", "4.25", "53.125"),
        )
        self.assertEqual(
            str(self.connection.execute(
                f"SELECT total_amount FROM {schema}.supplier_bills WHERE id=%s", (bill_id,)
            ).fetchone()["total_amount"]),
            "53.12500000",
        )

    def test_sales_schema_links_invoice_lines_receipts_and_credit_notes(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [])
        schema = quote_schema_name(self.schema)
        user_id = self.connection.execute(
            f"INSERT INTO {schema}.users(username,full_name,password_hash,role,created_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            ("sales_test", "Sales Test", "hash", "admin", "2026-01-01"),
        ).fetchone()["id"]
        account_id = self.connection.execute(
            f"INSERT INTO {schema}.accounts(code,name_ar,account_type) VALUES (%s,%s,%s) RETURNING id",
            ("1010", "Cash", "asset"),
        ).fetchone()["id"]
        customer_id = self.connection.execute(
            f"INSERT INTO {schema}.customers(code,name) VALUES (%s,%s) RETURNING id",
            ("C-PG-1", "Customer PG"),
        ).fetchone()["id"]
        invoice_id = self.connection.execute(
            f"INSERT INTO {schema}.sales_invoices(invoice_no,customer_id,invoice_date,subtotal,total_amount,created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            ("SI-PG-1", customer_id, "2026-01-05", "100.125", "115.14375", user_id),
        ).fetchone()["id"]
        product_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_products(sku,name_ar) VALUES (%s,%s) RETURNING id",
            ("SKU-SALES-1", "Sales Product"),
        ).fetchone()["id"]
        line_id = self.connection.execute(
            f"INSERT INTO {schema}.sales_invoice_lines(invoice_id,product_id,description,quantity,unit_price,"
            "line_total,cost_total) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (invoice_id, product_id, "Sales Product", "2.5", "40.05", "100.125", "62.5"),
        ).fetchone()["id"]
        receipt_id = self.connection.execute(
            f"INSERT INTO {schema}.customer_receipts(receipt_no,invoice_id,receipt_date,amount,cash_account_id,created_by) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            ("CR-PG-1", invoice_id, "2026-01-06", "50", account_id, user_id),
        ).fetchone()["id"]
        credit_id = self.connection.execute(
            f"INSERT INTO {schema}.sales_credit_notes(credit_note_no,invoice_id,credit_date,reason,subtotal,"
            "tax_amount,total_amount) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            ("CN-PG-1", invoice_id, "2026-01-07", "Return", "20", "3", "23"),
        ).fetchone()["id"]
        self.connection.execute(
            f"INSERT INTO {schema}.sales_credit_note_lines(credit_note_id,invoice_line_id,product_id,description,"
            "quantity,unit_price,line_total,tax_rate,tax_treatment,tax_amount,cost_total) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (credit_id, line_id, product_id, "Returned item", "0.5", "40", "20", "15", "standard", "3", "12.5"),
        )
        self.assertEqual(
            str(self.connection.execute(
                f"SELECT amount FROM {schema}.customer_receipts WHERE id=%s", (receipt_id,)
            ).fetchone()["amount"]),
            "50.00000000",
        )

    def test_inventory_schema_uses_exact_quantities_and_enforces_location_scope(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [])
        schema = quote_schema_name(self.schema)

        warehouse_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_warehouses(code,name_ar) VALUES (%s,%s) RETURNING id",
            ("WH-01", "Main Warehouse"),
        ).fetchone()["id"]
        other_warehouse_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_warehouses(code,name_ar) VALUES (%s,%s) RETURNING id",
            ("WH-02", "Secondary Warehouse"),
        ).fetchone()["id"]
        product_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_products(sku,name_ar,purchase_price) "
            "VALUES (%s,%s,%s) RETURNING id",
            ("SKU-001", "Test Product", "12.345678"),
        ).fetchone()["id"]
        location_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_locations(warehouse_id,code,name_ar,location_type) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (warehouse_id, "F-01", "Floor 1", "floor"),
        ).fetchone()["id"]
        self.connection.execute(
            f"INSERT INTO {schema}.stock_product_locations(product_id,warehouse_id,location_id) "
            "VALUES (%s,%s,%s)",
            (product_id, warehouse_id, location_id),
        )

        movement_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_movements(movement_no,movement_type,product_id,"
            "target_warehouse_id,quantity,unit_cost,total_cost,movement_date) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            ("RCV-001", "receipt", product_id, warehouse_id, "2.125", "12.345678", "26.23456575", "2026-01-01"),
        ).fetchone()["id"]
        amount = self.connection.execute(
            f"SELECT unit_cost FROM {schema}.stock_movements WHERE id=%s",
            (movement_id,),
        ).fetchone()["unit_cost"]
        self.assertEqual(str(amount), "12.345678")
        layer_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_valuation_layers(product_id,warehouse_id,source_movement_id,"
            "qty_in,qty_remaining,unit_cost) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (product_id, warehouse_id, movement_id, "2.125", "2.125", "12.345678"),
        ).fetchone()["id"]
        layer_value = self.connection.execute(
            f"SELECT value_remaining FROM {schema}.stock_valuation_layers WHERE id=%s",
            (layer_id,),
        ).fetchone()["value_remaining"]
        self.assertEqual(str(layer_value), "26.23456575")
        # Keep the valid fixture rows while testing each rejected insert in isolation.
        self.connection.commit()

        with self.assertRaises(Exception):
            self.connection.execute(
                f"INSERT INTO {schema}.stock_movements(movement_no,movement_type,product_id,"
                "target_warehouse_id,quantity,movement_date,entered_uom_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("RCV-BAD-UOM", "receipt", product_id, warehouse_id, "1", "2026-01-02", 999999),
            )
        self.connection.rollback()

        with self.assertRaises(Exception):
            self.connection.execute(
                f"INSERT INTO {schema}.stock_product_locations(product_id,warehouse_id,location_id) "
                "VALUES (%s,%s,%s)",
                (product_id, other_warehouse_id, location_id),
            )
        self.connection.rollback()

        inactive_location_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_locations(warehouse_id,code,name_ar,location_type,active) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (other_warehouse_id, "F-02", "Inactive Floor", "floor", 0),
        ).fetchone()["id"]
        with self.assertRaises(Exception):
            self.connection.execute(
                f"INSERT INTO {schema}.stock_product_locations(product_id,warehouse_id,location_id) "
                "VALUES (%s,%s,%s)",
                (product_id, other_warehouse_id, inactive_location_id),
            )
        self.connection.rollback()

        unit_count = self.connection.execute(
            f"SELECT COUNT(*) AS count FROM {schema}.stock_uoms"
        ).fetchone()["count"]
        self.assertGreaterEqual(unit_count, 18)

    def test_movement_uom_fk_preserves_legacy_rows_but_blocks_new_invalid_references(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual(apply_migrations(self.connection, migrations[:7], schema=self.schema),
                         [1, 2, 3, 4, 5, 6, 7])
        schema = quote_schema_name(self.schema)
        product_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_products(sku,name_ar) VALUES (%s,%s) RETURNING id",
            ("SKU-LEGACY-UOM", "Legacy UOM Product"),
        ).fetchone()["id"]
        warehouse_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_warehouses(code,name_ar) VALUES (%s,%s) RETURNING id",
            ("WH-LEGACY-UOM", "Legacy UOM Warehouse"),
        ).fetchone()["id"]
        legacy_movement_id = self.connection.execute(
            f"INSERT INTO {schema}.stock_movements(movement_no,movement_type,product_id,"
            "target_warehouse_id,quantity,movement_date,entered_uom_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            ("RCV-LEGACY-UOM", "receipt", product_id, warehouse_id, "1", "2026-01-01", 999999),
        ).fetchone()["id"]
        self.connection.commit()

        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema), [8, 9])
        preserved = self.connection.execute(
            f"SELECT entered_uom_id FROM {schema}.stock_movements WHERE id=%s",
            (legacy_movement_id,),
        ).fetchone()
        self.assertEqual(preserved["entered_uom_id"], 999999)
        self.connection.commit()

        with self.assertRaises(Exception):
            self.connection.execute(
                f"INSERT INTO {schema}.stock_movements(movement_no,movement_type,product_id,"
                "target_warehouse_id,quantity,movement_date,entered_uom_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("RCV-NEW-BAD-UOM", "receipt", product_id, warehouse_id, "1", "2026-01-02", 999998),
            )
        self.connection.rollback()

    def test_supplier_return_pricing_schema_preserves_invoice_line_links(self):
        migration_dir = Path(__file__).resolve().parents[1] / "postgres_migrations"
        migrations = load_migrations(migration_dir)
        self.assertEqual(apply_migrations(self.connection, migrations, schema=self.schema),
                         [1, 2, 3, 4, 5, 6, 7, 8, 9])
        columns = self.connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (self.schema, "supplier_credit_note_lines"),
        ).fetchall()
        self.assertIn("supplier_total", {row["column_name"] for row in columns})
        table = self.connection.execute(
            "SELECT to_regclass(%s) AS table_name",
            (self.schema + ".supplier_credit_note_bill_line_allocations",),
        ).fetchone()
        self.assertIsNotNone(table["table_name"])
        constraints = self.connection.execute(
            "SELECT COUNT(*) AS count FROM pg_constraint "
            "WHERE conrelid=to_regclass(%s) AND contype='f'",
            (self.schema + ".supplier_credit_note_bill_line_allocations",),
        ).fetchone()
        self.assertEqual(constraints["count"], 2)

    def test_failed_batch_rolls_back_schema_and_migration_records(self):
        migrations = [
            self._migration(1, "0001_ok.sql", 'CREATE TABLE "first_probe" (id BIGINT)'),
            self._migration(2, "0002_fail.sql", 'CREATE TABLE "broken_probe" ('),
        ]
        with self.assertRaises(Exception):
            apply_migrations(self.connection, migrations, schema=self.schema)
        exists = self.connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s) AS present",
            (self.schema,),
        ).fetchone()["present"]
        self.assertFalse(exists)


if __name__ == "__main__":
    unittest.main()
