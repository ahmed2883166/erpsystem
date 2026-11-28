# Landed Cost Design — Purchasing

## Scope

This change adds landed-cost capitalization for freight, customs, insurance, clearance, and other directly attributable acquisition costs. It does not create another supplier payable: the supplier invoice remains the sole AP document and is posted through the existing direct-expense purchase-invoice workflow.

## Source document and controls

- A landed-cost document must reference a posted direct-expense supplier invoice from the same company. The source vendor may be a freight forwarder, customs broker, insurer, or other service supplier and need not be the goods supplier.
- Only invoice lines with an active, detail-level expense account that permits posting are eligible as the credit source for reclassification.
- Every allocation references one posted purchase-receipt line and one eligible expense invoice line.
- Allocation totals must not exceed the eligible expense-line amount; a posted source invoice line cannot be capitalized twice beyond its unallocated balance.
- Documents are immutable after posting. Corrections use a controlled reversal, not edits or deletion.
- The service locks the landed-cost document, source invoice lines, receipt lines, cost layers, and affected stock balances in deterministic order.

## Allocation basis

1. Remaining stock quantity is grouped by warehouse, storage location, and expiry date using the original root cost layers.
2. Final consumption is classified by movement: issue/sales consumption to COGS; damage, expiry, and write-off adjustments to inventory adjustment loss.
3. Internal transfers are not treated as final consumption. The child layer remains part of the original receipt lineage and receives the revised unit cost.
4. The sum of remaining quantity and final-consumed quantity must equal the receipt quantity. Missing or inconsistent lineage blocks posting.
5. Amounts are rounded to the company's base-currency precision; any residual is assigned deterministically to the largest unrounded bucket. The allocation must reconcile exactly to its source amount.
6. Allocation supports automatic distribution by receipt-line value, quantity, total weight, or total volume, with deterministic rounding so allocated rows reconcile exactly to the entered total. Manual allocation remains available. Weight is stored in kilograms (kg) and volume in cubic metres (m³) for every product, so the measurements are comparable across receipts. The per-unit measurements are multiplied by each received quantity; a zero measurement blocks that basis instead of silently falling back to another method.

## Cost and accounting effects

- Remaining-stock share: increase stock balance inventory value, recompute average cost, and increase the unit cost of every remaining descendant cost layer in the receipt lineage.
- Consumed share: debit COGS or inventory-adjustment loss by the branch where final consumption occurred.
- Credit: the exact expense account from the source direct-expense invoice line. This reclassifies the already-posted expense; it does not debit AP, credit GRNI, or create a second supplier invoice.
- Each affected branch receives a separately posted journal entry. Inter-branch balancing must use the configured inter-branch clearing role where the source expense branch differs from the inventory/consumption branch.
- Every generated journal must balance, use an open fiscal year for its posting date, and be linked uniquely to the landed-cost allocation. A failed posting rolls back stock valuation and all journals in the same database transaction.

## Required acceptance tests

- Full quantity still on hand: 100% capitalized to inventory; no COGS/loss.
- Partially issued quantity: split between inventory and COGS.
- Damaged/expired/written-off quantity: split to adjustment loss.
- Multi-warehouse and inter-branch transfers: preserve root lineage and allocate to the final holding/consuming branch.
- FIFO and weighted-average costing: update both cost layers and stock balances without changing quantity.
- Multiple landed-cost documents against one supplier invoice line: cumulative amount cannot exceed the invoice-line amount.
- Duplicate posting, concurrent posting, closed fiscal period, wrong company/supplier, draft source invoice, and incomplete cost lineage are rejected.
- Supplier AP, GRNI, receipt quantity, and original purchase invoice journal are unchanged by landed-cost posting.
- Inventory subledger equals inventory GL; COGS/loss allocation equals its journals.

## Purchasing reports to complete

1. Supplier and item purchase analysis by date range, activity, branch, and warehouse.
2. Purchase order → receipt → invoice matching, including open/partial quantities and price/quantity variances.
3. Supplier returns and credit/refund history.
4. Landed-cost register by source invoice, expense type, receipt, item, warehouse, and branch.
5. Reconciliation of landed cost allocated, unallocated eligible source-invoice balance, inventory capitalization, COGS, and adjustment loss.
6. CSV/XLSX/PDF export with the same filters and totals as the on-screen report.

## Delivery gate

Do not merge until model constraints, migrations, permission-scoped API, immutable posting/reversal services, report endpoints/UI, PostgreSQL migrations, and the acceptance suite are implemented and GitHub Actions pass on a pull request targeting main.