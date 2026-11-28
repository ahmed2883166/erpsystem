# Audit findings and verification log

Audit branch: `feature/comprehensive-erp-audit-2026-11-28`
Base commit: `9682a4b98892dfdfe6d32301b2a165e4d0f8860f`

## Initial verified observations

### Inventory negative-stock control
- The Django inventory posting service rejects an issue when the locked stock balance is lower than the requested quantity.
- The posting path is wrapped in `transaction.atomic`; the movement is marked posted only after its lines, costing and journal effects complete.
- The backend inventory API regression test `test_issue_cannot_make_stock_negative_and_is_atomic` verifies that an issue from a zero-balance warehouse is rejected, no negative balance is created, and the movement remains draft.
- The sales delivery posting path delegates to the same inventory posting service, so a goods delivery cannot be posted from zero/insufficient stock through that path.
- No change is made to the negative-stock policy in this audit's initial pass. Any future exception must be explicitly designed and authorized.

Status: verified by source inspection and existing test coverage; broader sales and multi-branch end-to-end scenarios remain in the audit scope.


### Treasury account branch-scope enforcement
- Confirmed: `TreasuryAccountViewSet.get_queryset` hides unassigned/company-wide treasury accounts from users who cannot access every active branch, but the create path previously allowed those users to create an account with `branch=null`.
- Confirmed: the generic update path previously allowed an accessible branch account with no financial history to be changed to `branch=null` without checking company-wide branch access.
- Fix on this audit branch: require company-wide branch access for creating or changing an account to an unassigned/company-wide scope; re-check company and write role on update; prohibit moving an existing treasury account to another company.
- Regression tests added: branch-limited users cannot create a company-wide treasury account and cannot change a branch treasury account to company-wide scope.
- Validation: CI, PostgreSQL backend suite, CodeQL and concurrent-user workflow are running on the fix commit; result pending.

## Open review areas
- Purchase -> partial receipt -> invoice -> payment -> GL and GRNI reconciliation.
- Sales -> delivery -> invoice -> COGS -> collection -> customer statement and reports.
- Sales/purchase returns, reversal idempotency and period-close enforcement.
- Inter-branch dispatch and partial receipt with independent source/destination permissions.
- Inventory valuation, landed costs, negative/zero stock, expiry, count and warehouse deactivation.
- Trial balance and financial statement reconciliation, indirect cash flow and subledgers.
- Treasury cash/bank balances, statement matching, backdated transactions and reversal behavior.
- Fixed asset acquisition, depreciation, impairment, transfer and disposal.
- Company/branch data isolation and authorization across all API endpoints.
- Audit logging, backup/restore, concurrency, performance, Windows/LAN and frontend behavior.

No production database was accessed or modified. No destructive data or schema operation was run.
