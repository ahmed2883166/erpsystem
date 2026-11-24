from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from accounting.models import ChartOfAccount, JournalEntry
from audit.models import AuditEvent


class FiscalYearJournalSafetyTests(APITestCase):
    def setUp(self):
        self.currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(
            code="FY-JOURNAL", name_ar="اختبار قيود السنة", country="EG",
            activity="commercial", base_currency=self.currency,
        )
        self.branch = Branch.objects.create(
            company=self.company, code="MAIN", name_ar="الفرع الرئيسي"
        )
        self.year = FiscalYear.objects.create(
            company=self.company, name="2026",
            start_date="2026-01-01", end_date="2026-12-31",
        )
        self.admin = get_user_model().objects.create_user(
            username="fiscal-journal-admin", password="test-password"
        )
        CompanyMembership.objects.create(
            user=self.admin, company=self.company, role="admin", all_branches=True,
        )
        self.asset = ChartOfAccount.objects.create(
            company=self.company, code="1000", name_ar="نقدية", nature="asset",
            is_group=False, allow_posting=True,
        )
        self.equity = ChartOfAccount.objects.create(
            company=self.company, code="3000", name_ar="حقوق ملكية", nature="equity",
            is_group=False, allow_posting=True,
        )
        self.client.force_authenticate(self.admin)

    def payload(self, source_type, entry_date):
        return {
            "company": self.company.pk,
            "branch": self.branch.pk,
            "fiscal_year": self.year.pk,
            "entry_number": f"{source_type.upper()}-001",
            "entry_date": entry_date,
            "description": "قيد اختبار السنة المالية",
            "source_type": source_type,
            "lines": [
                {"account": self.asset.pk, "debit": "100.0000", "credit": "0"},
                {"account": self.equity.pk, "debit": "0", "credit": "100.0000"},
            ],
        }

    def test_journal_list_uses_100_row_pages_and_summary_is_unpaginated(self):
        JournalEntry.objects.bulk_create([
            JournalEntry(
                company=self.company, branch=self.branch, fiscal_year=self.year,
                entry_number=f"PAGE-{index:03d}", entry_date="2026-06-01",
                description="اختبار ترقيم القيود",
            )
            for index in range(105)
        ])

        page = self.client.get("/api/accounting/journal-entries/?company=%s&page_size=100" % self.company.pk)
        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual(page.data["count"], 105)
        self.assertEqual(len(page.data["results"]), 100)
        self.assertIsNotNone(page.data["next"])
        self.assertIsNone(page.data["previous"])

        summary = self.client.get("/api/accounting/journal-entries/summary/?company=%s" % self.company.pk)
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data, {"total": 105, "drafts": 105, "posted": 0})

    def test_company_admin_can_create_closing_draft_only_on_year_end(self):
        response = self.client.post(
            "/api/accounting/journal-entries/",
            self.payload(JournalEntry.Source.CLOSING, "2026-12-31"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["source_type"], JournalEntry.Source.CLOSING)

    def test_opening_and_closing_journals_reject_wrong_dates(self):
        response = self.client.post(
            "/api/accounting/journal-entries/",
            self.payload(JournalEntry.Source.CLOSING, "2026-12-30"),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(entry_number="CLOSING-001").exists())

    def test_accountant_cannot_create_opening_or_closing_journals(self):
        accountant = get_user_model().objects.create_user(
            username="fiscal-journal-accountant", password="test-password"
        )
        CompanyMembership.objects.create(
            user=accountant, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(accountant)
        response = self.client.post(
            "/api/accounting/journal-entries/",
            self.payload(JournalEntry.Source.OPENING, "2026-01-01"),
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(JournalEntry.objects.filter(source_type=JournalEntry.Source.OPENING).exists())

    def test_closed_fiscal_year_rejects_new_journal_drafts(self):
        self.year.status = FiscalYear.Status.CLOSED
        self.year.save(update_fields=("status",))
        response = self.client.post(
            "/api/accounting/journal-entries/",
            self.payload(JournalEntry.Source.MANUAL, "2026-06-01"),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(JournalEntry.objects.filter(entry_number="MANUAL-001").exists())

    def test_duplicate_closing_draft_is_rejected_for_same_branch_and_year(self):
        first = self.client.post(
            "/api/accounting/journal-entries/",
            self.payload(JournalEntry.Source.CLOSING, "2026-12-31"),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)
        second_payload = self.payload(JournalEntry.Source.CLOSING, "2026-12-31")
        second_payload["entry_number"] = "CLOSING-002"
        second = self.client.post(
            "/api/accounting/journal-entries/", second_payload, format="json"
        )
        self.assertEqual(second.status_code, 400, second.data)
        self.assertEqual(
            JournalEntry.objects.filter(
                fiscal_year=self.year, branch=self.branch,
                source_type=JournalEntry.Source.CLOSING,
            ).count(),
            1,
        )

    def test_existing_draft_cannot_be_posted_after_fiscal_year_is_closed(self):
        from accounting.models import JournalLine
        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="DRAFT-BEFORE-CLOSE", entry_date="2026-06-01",
            description="مسودة قبل الإقفال", source_type=JournalEntry.Source.MANUAL,
        )
        JournalLine.objects.create(
            entry=entry, account=self.asset, debit="100.0000", credit="0",
        )
        JournalLine.objects.create(
            entry=entry, account=self.equity, debit="0", credit="100.0000",
        )
        self.year.status = FiscalYear.Status.CLOSED
        self.year.save(update_fields=("status",))
        response = self.client.post(
            f"/api/accounting/journal-entries/{entry.pk}/post/", {}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)
        entry.refresh_from_db()
        self.assertEqual(entry.status, JournalEntry.Status.DRAFT)

    def test_posting_row_lock_does_not_lock_related_company_or_branch_rows(self):
        from accounting.models import JournalLine

        entry = JournalEntry.objects.create(
            company=self.company, branch=self.branch, fiscal_year=self.year,
            entry_number="LOCK-SCOPE-001", entry_date="2026-06-01",
            description="اختبار نطاق قفل الترحيل",
        )
        JournalLine.objects.create(
            entry=entry, account=self.asset, debit="100.0000", credit="0",
        )
        JournalLine.objects.create(
            entry=entry, account=self.equity, debit="0", credit="100.0000",
        )

        with CaptureQueriesContext(connection) as captured:
            posted = self.client.post(
                f"/api/accounting/journal-entries/{entry.pk}/post/", {}, format="json"
            )
        self.assertEqual(posted.status_code, 200, posted.data)
        self.assertEqual(posted.data["status"], JournalEntry.Status.POSTED)

        if connection.vendor == "postgresql":
            lock_queries = [
                query["sql"] for query in captured
                if "FOR UPDATE" in query["sql"].upper()
                and "accounting_journalentry" in query["sql"]
            ]
            self.assertTrue(lock_queries, "Expected the journal posting row lock query.")
            self.assertTrue(
                all("FOR UPDATE OF" in sql.upper() for sql in lock_queries),
                lock_queries,
            )

    def create_and_post(self, source_type, number, date, lines):
        payload = {
            "company": self.company.pk,
            "branch": self.branch.pk,
            "fiscal_year": self.year.pk,
            "entry_number": number,
            "entry_date": date,
            "description": "اختبار ترحيل قيد السنة",
            "source_type": source_type,
            "lines": lines,
        }
        created = self.client.post(
            "/api/accounting/journal-entries/", payload, format="json"
        )
        self.assertEqual(created.status_code, 201, created.content)
        posted = self.client.post(
            f"/api/accounting/journal-entries/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.content)
        return posted.data

    def test_closing_review_accepts_a_posted_closing_entry_that_clears_pnl(self):
        revenue = ChartOfAccount.objects.create(
            company=self.company, code="4000", name_ar="إيرادات", nature="revenue",
            is_group=False, allow_posting=True,
        )
        expense = ChartOfAccount.objects.create(
            company=self.company, code="5000", name_ar="مصروفات", nature="expense",
            is_group=False, allow_posting=True,
        )
        self.create_and_post("manual", "REV-001", "2026-05-01", [
            {"account": self.asset.pk, "debit": "100.0000", "credit": "0"},
            {"account": revenue.pk, "debit": "0", "credit": "100.0000"},
        ])
        self.create_and_post("manual", "EXP-001", "2026-06-01", [
            {"account": expense.pk, "debit": "40.0000", "credit": "0"},
            {"account": self.asset.pk, "debit": "0", "credit": "40.0000"},
        ])
        self.create_and_post(JournalEntry.Source.CLOSING, "CLS-001", "2026-12-31", [
            {"account": revenue.pk, "debit": "100.0000", "credit": "0"},
            {"account": expense.pk, "debit": "0", "credit": "40.0000"},
            {"account": self.equity.pk, "debit": "0", "credit": "60.0000"},
        ])
        review = self.client.get(
            f"/api/setup/fiscal-years/{self.year.pk}/closing_review/"
        )
        self.assertEqual(review.status_code, 200, review.content)
        self.assertTrue(review.data["can_close"], review.data)
        self.assertEqual(review.data["unclosed_income_statement_accounts"], 0)
        trial_balance = self.client.get(
            f"/api/accounting/journal-entries/trial-balance/?company={self.company.pk}"
        )
        self.assertEqual(trial_balance.status_code, 200, trial_balance.content)
        self.assertEqual(Decimal(str(trial_balance.data["difference"])), Decimal("0"))
        pnl_rows = [
            row for row in trial_balance.data["accounts"]
            if row["account_id"] in (revenue.pk, expense.pk)
        ]
        self.assertTrue(all(Decimal(str(row["closing_balance"])) == 0 for row in pnl_rows))
        closed = self.client.post(
            f"/api/setup/fiscal-years/{self.year.pk}/close/", {}, format="json"
        )
        self.assertEqual(closed.status_code, 200, closed.content)
        self.year.refresh_from_db()
        self.assertEqual(self.year.status, FiscalYear.Status.CLOSED)

    def test_next_year_opening_must_match_prior_year_balance_sheet(self):
        revenue = ChartOfAccount.objects.create(
            company=self.company, code="4000", name_ar="إيرادات", nature="revenue",
            is_group=False, allow_posting=True,
        )
        next_year = FiscalYear.objects.create(
            company=self.company, name="2027",
            start_date="2027-01-01", end_date="2027-12-31",
        )
        self.create_and_post("manual", "REV-002", "2026-05-01", [
            {"account": self.asset.pk, "debit": "100.0000", "credit": "0"},
            {"account": revenue.pk, "debit": "0", "credit": "100.0000"},
        ])
        self.create_and_post(JournalEntry.Source.CLOSING, "CLS-002", "2026-12-31", [
            {"account": revenue.pk, "debit": "100.0000", "credit": "0"},
            {"account": self.equity.pk, "debit": "0", "credit": "100.0000"},
        ])
        opening = self.client.post(
            "/api/accounting/journal-entries/",
            {
                "company": self.company.pk, "branch": self.branch.pk,
                "fiscal_year": next_year.pk, "entry_number": "OPEN-2027",
                "entry_date": "2027-01-01", "description": "افتتاح السنة الجديدة",
                "source_type": JournalEntry.Source.OPENING,
                "lines": [
                    {"account": self.asset.pk, "debit": "99.0000", "credit": "0"},
                    {"account": self.equity.pk, "debit": "0", "credit": "99.0000"},
                ],
            }, format="json",
        )
        self.assertEqual(opening.status_code, 201, opening.content)
        posted = self.client.post(
            f"/api/accounting/journal-entries/{opening.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.content)
        review = self.client.get(
            f"/api/setup/fiscal-years/{self.year.pk}/closing_review/"
        )
        self.assertEqual(review.status_code, 200, review.content)
        self.assertEqual(review.data["opening_balance_mismatches"], 2)
        self.assertFalse(review.data["can_close"])


    def test_journal_post_audit_records_the_actual_poster_not_the_draft_creator(self):
        created = self.client.post(
            "/api/accounting/journal-entries/",
            self.payload(JournalEntry.Source.MANUAL, "2026-06-15"),
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        entry_id = created.data["id"]

        poster = get_user_model().objects.create_user(
            username="fiscal-journal-poster", password="test-password"
        )
        CompanyMembership.objects.create(
            user=poster, company=self.company, role="accountant", all_branches=True,
        )
        self.client.force_authenticate(poster)
        posted = self.client.post(
            f"/api/accounting/journal-entries/{entry_id}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)

        event = AuditEvent.objects.get(
            resource="accounting.journal_entry.posted",
            object_id=str(entry_id),
        )
        self.assertEqual(event.actor_id, poster.pk)
        self.assertNotEqual(event.actor_id, self.admin.pk)


    def test_correct_next_year_opening_matches_prior_year_balance_sheet_and_allows_close(self):
        revenue = ChartOfAccount.objects.create(
            company=self.company, code="4000", name_ar="إيرادات", nature="revenue",
            is_group=False, allow_posting=True,
        )
        next_year = FiscalYear.objects.create(
            company=self.company, name="2027",
            start_date="2027-01-01", end_date="2027-12-31",
        )
        self.create_and_post("manual", "REV-OPEN-TEST", "2026-05-01", [
            {"account": self.asset.pk, "debit": "100.0000", "credit": "0"},
            {"account": revenue.pk, "debit": "0", "credit": "100.0000"},
        ])
        self.create_and_post(JournalEntry.Source.CLOSING, "CLS-OPEN-TEST", "2026-12-31", [
            {"account": revenue.pk, "debit": "100.0000", "credit": "0"},
            {"account": self.equity.pk, "debit": "0", "credit": "100.0000"},
        ])
        opening = self.client.post(
            "/api/accounting/journal-entries/",
            {
                "company": self.company.pk, "branch": self.branch.pk,
                "fiscal_year": next_year.pk, "entry_number": "OPEN-MATCH-2027",
                "entry_date": "2027-01-01", "description": "افتتاح مطابق للميزانية",
                "source_type": JournalEntry.Source.OPENING,
                "lines": [
                    {"account": self.asset.pk, "debit": "100.0000", "credit": "0"},
                    {"account": self.equity.pk, "debit": "0", "credit": "100.0000"},
                ],
            }, format="json",
        )
        self.assertEqual(opening.status_code, 201, opening.content)
        posted = self.client.post(
            f"/api/accounting/journal-entries/{opening.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.content)
        review = self.client.get(
            f"/api/setup/fiscal-years/{self.year.pk}/closing_review/"
        )
        self.assertEqual(review.status_code, 200, review.content)
        self.assertEqual(review.data["opening_balance_mismatches"], 0)
        self.assertTrue(review.data["can_close"], review.data)
        closed = self.client.post(
            f"/api/setup/fiscal-years/{self.year.pk}/close/", {}, format="json"
        )
        self.assertEqual(closed.status_code, 200, closed.content)


class AccountingReportPostgresProfileTests(APITestCase):
    def test_large_report_scope_uses_bounded_single_worker_postgres_profile(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL-specific report session settings.")
        from accounting.api import _accounting_report_work_mem

        with CaptureQueriesContext(connection) as captured:
            with _accounting_report_work_mem():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('work_mem')")
                    work_mem = cursor.fetchone()[0]
                    cursor.execute("SELECT current_setting('max_parallel_workers_per_gather')")
                    parallel_workers = cursor.fetchone()[0]
                    cursor.execute("SELECT current_setting('jit')")
                    jit = cursor.fetchone()[0]

        self.assertEqual(work_mem, "32MB")
        self.assertEqual(parallel_workers, "0")
        self.assertEqual(jit, "off")
        sql = " ".join(query["sql"] for query in captured.captured_queries).upper()
        self.assertIn("SET LOCAL WORK_MEM", sql)
        self.assertIn("SET LOCAL MAX_PARALLEL_WORKERS_PER_GATHER", sql)
        self.assertIn("SET LOCAL JIT", sql)
