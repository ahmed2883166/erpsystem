# NEXUS ERP — Unified Chart of Accounts

## Design rule

The chart structure is shared across activities. Account numbers identify the place of an account in the chart; stable `AccountRole` values identify its accounting purpose. Posting services and financial reports must use roles rather than hard-coded account numbers.

The activity selected on company setup determines the labels and primary revenue/direct-cost accounts created by the starter template. It does not change the meaning or numbering ranges of tax, insurance, assets, liabilities, or equity.

## Starter number ranges

| Range | Purpose |
| --- | --- |
| 1xxx | Assets |
| 11xx | Current assets, cash, banks, customers, inventory and input VAT |
| 12xx | Fixed assets and accumulated depreciation |
| 2xxx | Liabilities |
| 21xx | Current liabilities, suppliers, output VAT, VAT payable, GRNI, payroll and insurance liabilities |
| 3xxx | Equity, capital and retained earnings |
| 4xxx | Operating revenue; the primary revenue account label is activity-specific |
| 5xxx | Direct costs; the primary cost account label is activity-specific |
| 6xxx | Operating and administrative expenses, payroll expense and employer insurance expense |
| 7xxx | Other income and gains |

## Tax and social-insurance separation

These are distinct accounts and distinct roles:

- 1150 — VAT input (asset / recoverable input tax)
- 2120 — VAT output (liability / output tax)
- 2130 — VAT payable (settlement liability)
- 2160 — employee social-insurance share payable
- 2170 — employer social-insurance share payable
- 6130 — employer social-insurance expense

No tax or insurance rate is pre-filled by this template. Legal rates and effective dates are configured separately for the company's country.

## Activity labels

The starter revenue and direct-cost accounts use the following names:

- Commercial: goods sales revenue / cost of goods sold
- Service: service revenue / cost of services
- Medical: medical services revenue / medical services and supplies cost
- Restaurants: food and beverage revenue / food and beverage cost
- Industrial: product sales revenue / cost of products sold

The industrial template is for inventory and accounting only; it does not enable manufacturing orders, BOMs, or production conversion.

## Safe application

- Applying a template is company-scoped and requires company-admin access.
- Existing accounts are never renamed, deleted, re-parented, or assigned new codes by template application.
- Existing role mappings are never overwritten.
- Reapplying the same template is idempotent.
- A code collision with a different account meaning or hierarchy returns HTTP 409 and creates nothing.
- Account codes are unique within each company.
- Historical journal lines reference account IDs; posting logic should resolve accounts through roles, not code strings.
