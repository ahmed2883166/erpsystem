import unittest

from scripts.generate_postgres_schema import (
    default_sql,
    extract_check_constraints,
    generate_postgres_ddl,
    pg_type,
    short_identifier,
    POSTGRES_TYPE_OVERRIDES,
)


class PostgreSQLSchemaGeneratorTests(unittest.TestCase):
    def test_maps_sqlite_types_conservatively(self):
        self.assertEqual(pg_type({"type": "INTEGER"}), "BIGINT")
        self.assertEqual(pg_type({"type": "REAL"}), "DOUBLE PRECISION")
        self.assertEqual(pg_type({"type": "TEXT"}), "TEXT")
        self.assertEqual(pg_type({"type": ""}), "TEXT")
        self.assertEqual(default_sql("CURRENT_TIMESTAMP", "TEXT"), "(CURRENT_TIMESTAMP::text)")
        self.assertEqual(default_sql("CURRENT_TIMESTAMP", "BIGINT"), "CURRENT_TIMESTAMP")

    def test_uses_only_explicit_numeric_overrides(self):
        self.assertEqual(
            pg_type({"name": "debit", "type": "TEXT"}, "journal_lines"),
            "NUMERIC",
        )
        self.assertEqual(
            pg_type({"name": "credit", "type": "TEXT"}, "journal_lines"),
            "NUMERIC",
        )
        self.assertEqual(
            pg_type({"name": "rate_to_base", "type": "REAL"}, "currencies"),
            "NUMERIC",
        )
        self.assertEqual(
            pg_type({"name": "rate", "type": "REAL"}, "tax_rates"),
            "NUMERIC",
        )
        # Similar-looking identifiers and metadata must not be coerced.
        self.assertEqual(
            pg_type({"name": "tax_number", "type": "TEXT"}, "customers"),
            "TEXT",
        )
        self.assertEqual(
            pg_type({"name": "tax_roles_json", "type": "TEXT"}, "activity_profiles"),
            "TEXT",
        )
        self.assertEqual(
            pg_type({"name": "rate_date", "type": "TEXT"}, "fx_exchange_rates"),
            "TEXT",
        )
        self.assertEqual(
            set(POSTGRES_TYPE_OVERRIDES),
            {
                ("journal_lines", "debit"),
                ("journal_lines", "credit"),
                ("currencies", "rate_to_base"),
                ("tax_rates", "rate"),
            },
        )

    def test_ddl_applies_numeric_overrides_by_table_and_column(self):
        snapshot = {
            "tables": [
                {
                    "name": "journal_lines",
                    "columns": [
                        {"name": "debit", "type": "TEXT", "pk": 0},
                        {"name": "credit", "type": "TEXT", "pk": 0},
                        {"name": "amount", "type": "TEXT", "pk": 0},
                    ],
                    "foreign_keys": [],
                    "indexes": [],
                },
                {
                    "name": "currencies",
                    "columns": [
                        {"name": "rate_to_base", "type": "REAL", "pk": 0},
                    ],
                    "foreign_keys": [],
                    "indexes": [],
                },
                {
                    "name": "tax_rates",
                    "columns": [
                        {"name": "rate", "type": "REAL", "pk": 0},
                        {"name": "tax_number", "type": "TEXT", "pk": 0},
                    ],
                    "foreign_keys": [],
                    "indexes": [],
                },
            ]
        }
        ddl, review = generate_postgres_ddl(snapshot)
        self.assertIn('"debit" NUMERIC', ddl)
        self.assertIn('"credit" NUMERIC', ddl)
        self.assertIn('"rate_to_base" NUMERIC', ddl)
        self.assertIn('"rate" NUMERIC', ddl)
        self.assertIn('"tax_number" TEXT', ddl)
        self.assertIn('"amount" TEXT', ddl)
        self.assertIn("journal_lines.amount", review["text_numeric_candidates"])
        self.assertNotIn("journal_lines.debit", review["text_numeric_candidates"])
        self.assertNotIn("journal_lines.credit", review["text_numeric_candidates"])
        self.assertEqual(review["text_numeric_candidate_count"], 1)
        self.assertEqual(
            review["text_numeric_candidates_by_table"],
            {"journal_lines": ["amount"]},
        )

    def test_rejects_numeric_overrides_missing_from_schema_snapshot(self):
        snapshot = {
            "tables": [{
                "name": "journal_lines",
                "columns": [{"name": "debit", "type": "TEXT", "pk": 0}],
                "foreign_keys": [],
                "indexes": [],
            }]
        }
        with self.assertRaisesRegex(
            ValueError, "credit|currencies.rate_to_base|tax_rates.rate"
        ):
            generate_postgres_ddl(snapshot)

    def test_extracts_balanced_check_constraints(self):
        sql = """CREATE TABLE sample (
            id INTEGER PRIMARY KEY,
            status TEXT CHECK(status IN ('open','closed')),
            amount TEXT CHECK (amount <> '' AND length(amount) > 0)
        )"""
        checks = extract_check_constraints(sql)
        self.assertEqual(len(checks), 2)
        self.assertIn("'open','closed'", checks[0])
        self.assertIn("length(amount) > 0", checks[1])

    def test_shortens_identifiers_deterministically(self):
        long_name = "fk_" + ("very_long_table_and_column_name_" * 3)
        result = short_identifier(long_name)
        self.assertLessEqual(len(result), 63)
        self.assertEqual(result, short_identifier(long_name))
        self.assertNotEqual(result, short_identifier(long_name + "_different"))

    def test_generates_tables_checks_foreign_keys_unique_and_expression_indexes(self):
        snapshot = {
            "tables": [
                {
                    "name": "parent",
                    "sql": "CREATE TABLE parent (id INTEGER PRIMARY KEY, code TEXT NOT NULL COLLATE NOCASE UNIQUE)",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "pk": 1, "notnull": 0, "dflt_value": None},
                        {"name": "code", "type": "TEXT", "pk": 0, "notnull": 1, "dflt_value": None},
                    ],
                    "foreign_keys": [],
                    "indexes": [
                        {"name": "sqlite_autoindex_parent_1", "origin": "u", "unique": 1,
                         "columns": [{"name": "code"}]},
                    ],
                },
                {
                    "name": "child",
                    "sql": "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER, amount TEXT CHECK(amount <> ''))",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "pk": 1, "notnull": 0, "dflt_value": None},
                        {"name": "parent_id", "type": "INTEGER", "pk": 0, "notnull": 1, "dflt_value": None},
                        {"name": "amount", "type": "TEXT", "pk": 0, "notnull": 1, "dflt_value": "'0'"},
                    ],
                    "foreign_keys": [
                        {"table": "parent", "from": "parent_id", "to": "id",
                         "on_delete": "CASCADE", "on_update": "NO ACTION"},
                    ],
                    "indexes": [
                        {"name": "idx_child_amount", "origin": "c", "unique": 0,
                         "columns": [{"name": "amount"}]},
                        {"name": "idx_child_expr", "origin": "c", "unique": 1,
                         "columns": [{"name": None}, {"name": None}]},
                    ],
                },
            ],
            "indexes": [
                {"name": "idx_child_expr",
                 "sql": "CREATE UNIQUE INDEX idx_child_expr ON child(lower(trim(amount)))"},
            ],
            "triggers": [
                {"name": "audit_log_block_delete", "table_name": "audit_log"},
                {"name": "trg_dashboard_version_accounts_insert", "table_name": "accounts"},
                {"name": "trg_stock_product_locations_same_wh_insert", "table_name": "stock_product_locations"},
            ],
        }
        ddl, review = generate_postgres_ddl(snapshot)
        self.assertIn('"id" BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY', ddl)
        self.assertIn("CHECK (amount <> '')", ddl)
        self.assertIn("REFERENCES \"parent\" (\"id\")", ddl)
        self.assertIn("ON DELETE CASCADE", ddl)
        self.assertIn("CREATE UNIQUE INDEX idx_child_expr ON child(lower(trim(amount)));", ddl)
        self.assertIn('CREATE UNIQUE INDEX "uq_parent_code_nocase" ON "parent" (lower("code"));', ddl)
        self.assertEqual(review["table_count"], 2)
        self.assertEqual(review["foreign_key_count"], 1)
        self.assertEqual(review["trigger_count"], 3)
        self.assertIn('CREATE TRIGGER "audit_log_block_delete" BEFORE DELETE', ddl)
        self.assertIn("EXECUTE FUNCTION nexus_bump_dashboard_version()", ddl)
        self.assertIn("RAISE EXCEPTION 'موقع الصنف الافتراضي", ddl)
        self.assertIn("child.amount", review["text_numeric_candidates"])

    def test_rejects_untranslated_views_instead_of_silently_omitting_them(self):
        snapshot = {
            "tables": [{
                "name": "sample",
                "columns": [{"name": "id", "type": "INTEGER", "pk": 1}],
                "foreign_keys": [],
                "indexes": [],
            }],
            "views": [{
                "name": "sample_balance_view",
                "sql": "CREATE VIEW sample_balance_view AS SELECT id FROM sample",
            }],
        }
        with self.assertRaisesRegex(
            ValueError, "views are not translated.*sample_balance_view"
        ):
            generate_postgres_ddl(snapshot)

    def test_accepts_initialized_schema_snapshot_with_no_views(self):
        snapshot = {
            "tables": [{
                "name": "sample",
                "columns": [{"name": "id", "type": "INTEGER", "pk": 1}],
                "foreign_keys": [],
                "indexes": [],
            }],
            "views": [],
        }
        ddl, review = generate_postgres_ddl(snapshot)
        self.assertIn('CREATE TABLE "sample"', ddl)
        self.assertEqual(review["table_count"], 1)

    def test_rejects_foreign_key_to_unknown_table(self):
        snapshot = {
            "tables": [{
                "name": "child",
                "columns": [{"name": "parent_id", "type": "INTEGER", "pk": 0}],
                "foreign_keys": [{"table": "missing", "from": "parent_id", "to": "id"}],
                "indexes": [],
            }]
        }
        with self.assertRaisesRegex(ValueError, "references missing table"):
            generate_postgres_ddl(snapshot)


if __name__ == "__main__":
    unittest.main()
