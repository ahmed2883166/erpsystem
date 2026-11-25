# Architecture decisions

## ADR-001 — Independent rebuild
The new application is built in a separate `nexus_next/` directory. Legacy Flask modules and data remain untouched and are used only to understand expected workflows.

## ADR-002 — Modular monolith
One deployable Django backend with domain modules and clear ownership boundaries. No microservices at this stage.

Initial domains:
- identity: users, roles, permissions, audit events
- companies: companies, branches, fiscal years, currencies, activity profiles
- accounting: chart of accounts, journals, posting, periods, ledgers, financial statements
- parties: customers, suppliers, employees
- sales and purchasing
- inventory: warehouses, locations, items, receipts, issues, transfers, valuation
- treasury: cashboxes, banks, payments, receipts, reconciliation
- assets: fixed assets and depreciation
- payroll: manual payroll, incentives, overtime
- tax: Egypt and Saudi configuration and reconciliation workpapers
- reporting: statements, ageing, budgets, analytics, exports

## Accounting invariants
- Posted journal entries are immutable; corrections use linked reversals.
- Every posted journal balances in company currency.
- Closed periods reject ordinary posting.
- Subledgers reconcile to their control accounts.
- Inventory negative quantity is blocked.
- Account roles are semantic configuration, not hard-coded account numbers.

## Data safety
Use disposable PostgreSQL databases for tests. Never point tests at legacy `data/` files or production databases. No data migration is part of the initial build.

## Delivery sequence
1. Project foundation, configuration, health checks, CI and test database.
2. Identity, companies, branches, currencies, fiscal years and activity setup.
3. Accounting core and opening balances.
4. Parties, sales, purchasing, inventory and treasury.
5. Assets, payroll, tax workpapers and reports.
6. UX hardening, performance, backup/restore, security and LAN acceptance.
