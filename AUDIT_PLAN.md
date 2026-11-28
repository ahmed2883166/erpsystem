# AdmRail ERP Comprehensive Audit

Base: main at 9682a4b98892dfdfe6d32301b2a165e4d0f8860f

## Rules
- Keep all audit findings, fixes, and regression tests on this single feature branch.
- Do not modify main directly.
- Do not run destructive migrations, database resets, data deletion, or tests against a user's live database.
- Use isolated temporary test databases and preserve existing company data.
- Treat existing behavior as unverified until covered by code inspection and tests.
- Do not change HR/payroll business rules as part of this audit; inspect integration and export safety only.

## Review scope
1. Sales: quotes/orders/invoices, returns, discounts, credit limits, stock issue, tax, receipts and reversals.
2. Purchasing: requests/orders, supplier restrictions, partial/full receipts, invoices, returns, payments and approvals.
3. Inventory: stock availability and negative-stock policy, reservations, warehouses, branch transfers, partial receipt, adjustments, costing, COGS and reconciliation.
4. Accounting: journals, posting/reversal controls, fiscal periods, opening/closing, trial balance, P&L, balance sheet, equity, indirect cash flow, tax and subledgers.
5. Treasury/banking: receipts, payments, transfers, reconciliation, bank statement imports and cash controls.
6. Customers/suppliers: master data, balances, aging, credit/payment terms and branch access.
7. Fixed assets: acquisition, depreciation, impairment, transfers, disposal and GL/cash-flow links.
8. Companies/branches/activities: membership, permissions, data isolation, inter-branch controls and configuration.
9. HR boundary: verify access boundaries and export only; do not alter payroll or HR business logic.
10. Platform: authentication, authorization, audit trail, input validation, SQL safety, concurrency, performance, backup/restore, Windows/LAN deployment and frontend reliability.

## Required end-to-end scenarios
- Purchase -> partial receipt -> stock -> supplier invoice -> payment -> GL/reports.
- Stock -> sales invoice -> issue -> COGS/revenue/tax -> receipt -> customer statement/reports.
- Sales and purchase returns -> stock/accounting reversals and reconciliation.
- Inter-branch transfer -> dispatch -> partial receipt -> destination stock and GL.
- Zero/insufficient stock attempts, permissions and any explicitly configured exception.
- Multi-user concurrent posting, duplicate submission/idempotency, and period-close boundaries.
- Reconcile subledgers, stock valuation, trial balance and financial statements.

## Acceptance gates
- Each confirmed defect has a regression test.
- No unapproved destructive schema/data operations.
- Full backend/frontend checks and PostgreSQL isolated integration tests pass.
- Windows scripts and backup/restore checks pass.
- Security scan findings are reviewed; workflow success alone is not proof of zero alerts.
- Performance checks are recorded with dataset size and environment.
- No merge until all required checks pass and findings are summarized.
