from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from accounting.models import ChartOfAccount, JournalEntry
from approvals.models import DiscountApprovalPolicy, FinancialApprovalRequest
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from treasury.models import TreasuryAccount, TreasuryTransaction, BankReconciliation, BankStatementLine


class TreasuryWorkflowTests(APITestCase):
    def setUp(self):
        currency = Currency.objects.create(code="EGP", name_ar="جنيه مصري")
        self.company = Company.objects.create(code="TRS", name_ar="شركة الخزينة", country="EG", activity="commercial", base_currency=currency)
        self.branch = Branch.objects.create(company=self.company, code="MAIN", name_ar="الرئيسي", is_head_office=True)
        self.year = FiscalYear.objects.create(company=self.company, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
        self.user = get_user_model().objects.create_user(username="treasury-user", password="test-password")
        CompanyMembership.objects.create(user=self.user, company=self.company, role="accountant", all_branches=True)
        self.client.force_authenticate(self.user)
        self.cash_gl = ChartOfAccount.objects.create(company=self.company, code="1100", name_ar="الصندوق", nature=ChartOfAccount.Nature.ASSET)
        self.bank_gl = ChartOfAccount.objects.create(company=self.company, code="1110", name_ar="البنك", nature=ChartOfAccount.Nature.ASSET)
        self.revenue_gl = ChartOfAccount.objects.create(company=self.company, code="4100", name_ar="إيرادات أخرى", nature=ChartOfAccount.Nature.REVENUE)
        self.cash = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.cash_gl, kind="cash", code="CASH-1", name_ar="الخزينة الرئيسية")
        self.bank = TreasuryAccount.objects.create(company=self.company, branch=self.branch, account=self.bank_gl, kind="bank", code="BANK-1", name_ar="البنك الرئيسي", bank_name="بنك تجريبي")

    def _create(self, kind="receipt", **extra):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.cash.pk, "counter_account": self.revenue_gl.pk,
            "kind": kind, "document_number": "TR-DOC-001", "transaction_date": "2026-03-15",
            "amount": "250.0000", "description": "اختبار حركة خزينة",
        }
        payload.update(extra)
        return self.client.post("/api/treasury/transactions/", payload, format="json")

    def test_receipt_posts_balanced_journal_and_is_idempotent(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.data)
        doc_id = response.data["id"]
        posted = self.client.post(f"/api/treasury/transactions/{doc_id}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        entry = JournalEntry.objects.get(pk=posted.data["journal_entry"])
        self.assertEqual(entry.status, JournalEntry.Status.POSTED)
        lines = list(entry.lines.order_by("debit"))
        self.assertEqual(sum((line.debit for line in lines), Decimal("0")), Decimal("250.0000"))
        self.assertEqual(sum((line.credit for line in lines), Decimal("0")), Decimal("250.0000"))
        again = self.client.post(f"/api/treasury/transactions/{doc_id}/post/", {}, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(JournalEntry.objects.filter(pk=entry.pk).count(), 1)

    def test_transfer_posts_between_separate_cash_and_bank_ledgers(self):
        receipt = self._create(document_number="TR-SEED-001")
        self.assertEqual(receipt.status_code, 201, receipt.data)
        seeded = self.client.post(f"/api/treasury/transactions/{receipt.data['id']}/post/", {}, format="json")
        self.assertEqual(seeded.status_code, 200, seeded.data)
        response = self._create(
            kind="transfer", counter_account=None, destination_account=self.bank.pk,
            document_number="TR-DOC-002",
        )
        self.assertEqual(response.status_code, 201, response.data)
        posted = self.client.post(f"/api/treasury/transactions/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        entry = JournalEntry.objects.get(pk=posted.data["journal_entry"])
        self.assertEqual(set(entry.lines.values_list("account_id", flat=True)), {self.cash_gl.pk, self.bank_gl.pk})


    def test_limited_branch_user_cannot_reassign_treasury_account_to_another_branch(self):
        other_branch = Branch.objects.create(company=self.company, code="OTHER", name_ar="فرع آخر")
        limited = get_user_model().objects.create_user(username="treasury-branch-limited", password="test-password")
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.patch(
            f"/api/treasury/accounts/{self.bank.pk}/",
            {"branch": other_branch.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.branch_id, self.branch.pk)

    def test_limited_branch_user_cannot_reassign_draft_transaction_to_another_branch(self):
        created = self._create(document_number="TR-BRANCH-LOCK-001")
        self.assertEqual(created.status_code, 201, created.data)
        other_branch = Branch.objects.create(company=self.company, code="OTHER2", name_ar="فرع آخر")
        limited = get_user_model().objects.create_user(username="treasury-branch-limited-2", password="test-password")
        membership = CompanyMembership.objects.create(
            user=limited, company=self.company, role="accountant", all_branches=False
        )
        membership.branches.add(self.branch)
        self.client.force_authenticate(limited)
        response = self.client.patch(
            f"/api/treasury/transactions/{created.data['id']}/",
            {"branch": other_branch.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        doc = TreasuryTransaction.objects.get(pk=created.data["id"])
        self.assertEqual(doc.branch_id, self.branch.pk)

    def test_posted_treasury_transaction_cannot_be_deleted(self):
        created = self._create(document_number="TR-POSTED-DELETE-001")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(
            f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
        deleted = self.client.delete(f"/api/treasury/transactions/{created.data['id']}/")
        self.assertEqual(deleted.status_code, 400, deleted.data)
        self.assertTrue(TreasuryTransaction.objects.filter(pk=created.data["id"]).exists())

    def test_payment_requires_counter_account_and_positive_amount(self):
        invalid = self._create(kind="payment", amount="0")
        self.assertEqual(invalid.status_code, 400)

    def test_payment_posting_blocks_negative_treasury_balance(self):
        response = self._create(kind="payment", document_number="TR-PAY-001")
        self.assertEqual(response.status_code, 201, response.data)
        posted = self.client.post(f"/api/treasury/transactions/{response.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 400)
        self.assertIn("الرصيد المتاح غير كافٍ", str(posted.data))
        self.assertEqual(JournalEntry.objects.count(), 0)


    def test_bank_reconciliation_matches_statement_and_posts_no_extra_journal(self):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "BANK-REC-001", "transaction_date": "2026-03-10",
            "amount": "250.0000", "description": "تحصيل بنكي",
        }
        created = self.client.post("/api/treasury/transactions/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "ST-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "250.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        line = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/add_line/", {
            "line_number": 1, "transaction_date": "2026-03-10", "description": "تحصيل",
            "reference": "REF-1", "amount": "250.0000",
        }, format="json")
        self.assertEqual(line.status_code, 201, line.data)
        matched = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/match/", {
            "line_id": line.data["id"], "transaction_id": created.data["id"],
        }, format="json")
        self.assertEqual(matched.status_code, 200, matched.data)
        summary = self.client.get(f"/api/treasury/reconciliations/{recon.data['id']}/summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["unexplained_difference"], "0.0000")
        completed = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/reconcile/", {}, format="json")
        self.assertEqual(completed.status_code, 200, completed.data)
        self.assertEqual(completed.data["status"], "reconciled")
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_bank_reconciliation_rejects_wrong_amount_and_unmatched_lines(self):
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": self.bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "BANK-REC-002", "transaction_date": "2026-03-10",
            "amount": "100.0000", "description": "تحصيل",
        }
        created = self.client.post("/api/treasury/transactions/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.client.post(f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json")
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "ST-002",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "110.0000",
        }, format="json")
        line = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/add_line/", {
            "line_number": 1, "transaction_date": "2026-03-10", "amount": "110.0000",
        }, format="json")
        mismatch = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/match/", {
            "line_id": line.data["id"], "transaction_id": created.data["id"],
        }, format="json")
        self.assertEqual(mismatch.status_code, 400)
        finalize = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/reconcile/", {}, format="json")
        self.assertEqual(finalize.status_code, 400)


    def test_bank_match_rejects_transaction_from_unassigned_branch(self):
        shared_bank_gl = ChartOfAccount.objects.create(company=self.company, code="1120", name_ar="بنك مشترك", nature=ChartOfAccount.Nature.ASSET)
        shared_bank = TreasuryAccount.objects.create(company=self.company, branch=None, account=shared_bank_gl, kind="bank", code="BANK-SHARED", name_ar="بنك مشترك")
        payload = {
            "company": self.company.pk, "branch": self.branch.pk, "fiscal_year": self.year.pk,
            "source_account": shared_bank.pk, "counter_account": self.revenue_gl.pk,
            "kind": "receipt", "document_number": "BANK-BRANCH-001", "transaction_date": "2026-03-10",
            "amount": "50.0000", "description": "حركة فرع غير مصرح به",
        }
        created = self.client.post("/api/treasury/transactions/", payload, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        posted = self.client.post(f"/api/treasury/transactions/{created.data['id']}/post/", {}, format="json")
        self.assertEqual(posted.status_code, 200, posted.data)
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": shared_bank.pk, "statement_number": "BRANCH-ST-001",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "50.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        line = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/add_line/", {
            "line_number": 1, "transaction_date": "2026-03-10", "amount": "50.0000",
        }, format="json")
        self.assertEqual(line.status_code, 201, line.data)
        other_branch = Branch.objects.create(company=self.company, code="OTHER", name_ar="فرع آخر")
        limited = get_user_model().objects.create_user(username="treasury-limited", password="test-password")
        membership = CompanyMembership.objects.create(user=limited, company=self.company, role="accountant", all_branches=False)
        membership.branches.add(other_branch)
        self.client.force_authenticate(limited)
        matched = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/match/", {
            "line_id": line.data["id"], "transaction_id": created.data["id"],
        }, format="json")
        self.assertEqual(matched.status_code, 403, matched.data)
        summary = self.client.get(f"/api/treasury/reconciliations/{recon.data['id']}/summary/")
        self.assertEqual(summary.status_code, 200, summary.data)
        self.assertEqual(summary.data["book_balance"], "0.0000")
        self.assertEqual(summary.data["outstanding_transactions"], [])

    def test_bank_statement_csv_import_creates_lines_without_overwriting(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "CSV-001",
            "statement_date": "2026-03-31", "opening_balance": "100.0000", "closing_balance": "75.0000",
        }, format="json")
        self.assertEqual(recon.status_code, 201, recon.data)
        csv_file = SimpleUploadedFile(
            "statement.csv",
            b"date,amount,description,reference\n2026-03-02,50.00,Deposit,D-1\n2026-03-03,-75.00,Withdrawal,W-1\n",
            content_type="text/csv",
        )
        imported = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/import_csv/", {"file": csv_file}, format="multipart")
        self.assertEqual(imported.status_code, 201, imported.data)
        self.assertEqual(imported.data["imported"], 2)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 2)
        second = SimpleUploadedFile("again.csv", b"date,amount\n2026-03-04,1\n", content_type="text/csv")
        rejected = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/import_csv/", {"file": second}, format="multipart")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 2)

    def test_bank_statement_csv_import_rejects_invalid_rows_atomically(self):
        recon = self.client.post("/api/treasury/reconciliations/", {
            "company": self.company.pk, "bank_account": self.bank.pk, "statement_number": "CSV-002",
            "statement_date": "2026-03-31", "opening_balance": "0.0000", "closing_balance": "10.0000",
        }, format="json")
        csv_file = SimpleUploadedFile(
            "invalid.csv",
            b"date,amount\n2026-03-02,10.00\nnot-a-date,5.00\n",
            content_type="text/csv",
        )
        imported = self.client.post(f"/api/treasury/reconciliations/{recon.data['id']}/import_csv/", {"file": csv_file}, format="multipart")
        self.assertEqual(imported.status_code, 400)
        self.assertEqual(BankStatementLine.objects.filter(reconciliation_id=recon.data["id"]).count(), 0)


    def test_treasury_payment_over_limit_requires_manager_approval(self):
        DiscountApprovalPolicy.objects.create(
            company=self.company, max_payment_amount=Decimal("100"),
            created_by=self.user, updated_by=self.user,
        )
        seed = self._create(kind="receipt", document_number="TR-APPROVAL-SEED")
        self.assertEqual(seed.status_code, 201, seed.data)
        seeded = self.client.post(
            f"/api/treasury/transactions/{seed.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(seeded.status_code, 200, seeded.data)

        payment = self._create(
            kind="payment", document_number="TR-APPROVAL-PAY",
            amount="150", counter_account=self.revenue_gl.pk,
        )
        self.assertEqual(payment.status_code, 201, payment.data)
        request = FinancialApprovalRequest.objects.get(treasury_transaction_id=payment.data["id"])
        blocked = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)

        manager = get_user_model().objects.create_user(username="treasury-approval-admin", password="test-password")
        CompanyMembership.objects.create(user=manager, company=self.company, role="admin", all_branches=True)
        self.client.force_authenticate(manager)
        approved = self.client.post(
            f"/api/approvals/financial-requests/{request.pk}/approve/",
            {"note": "اعتماد سند صرف خزينة"}, format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(self.user)
        posted = self.client.post(
            f"/api/treasury/transactions/{payment.data['id']}/post/", {}, format="json"
        )
        self.assertEqual(posted.status_code, 200, posted.data)
