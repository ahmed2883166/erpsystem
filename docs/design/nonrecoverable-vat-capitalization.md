# Non-recoverable Input VAT Capitalization — Design

Status: design baseline; implementation is intentionally gated until the inventory costing and asset-acquisition paths are covered by tests.

## Accounting policy

A positive-rate input tax code with `input_tax_recoverable = false` must not be posted to the recoverable VAT input account and must not be silently expensed. Its destination follows the underlying purchase:
- Inventory goods: capitalize the non-recoverable portion into inventory cost to the extent units remain on hand; allocate the portion attributable to units already issued to cost of goods sold.
- Fixed asset: include the non-recoverable portion in the asset's initial cost and depreciable basis when directly attributable to bringing the asset to its intended condition/location.
- Expense/service: expense only where the underlying item is an expense and the tax is not part of another asset's cost.

Tax reporting must keep recoverable input VAT separate from non-recoverable tax. The supplier payable remains the gross invoice amount.

## Verified current implementation constraints

- Purchase invoice lines are one-to-one with stock receipt movement lines; the current purchase flow is a goods-receipt flow, not a general service invoice flow.
- The receipt has already posted inventory / GRNI before the supplier invoice is posted.
- Purchase invoice posting currently debits GRNI for net, credits AP for gross, and debits the VAT input role for all positive tax. The validation guard currently rejects positive-rate non-recoverable tax, preventing that incorrect posting.
- FIFO cost layers are created on receipt and consumed on issue. A transfer currently creates a new cost layer sourced from the transfer movement line, so the original receipt provenance is lost.
- Weighted-average costing is maintained on StockBalance; there is no per-receipt historical allocation for later capitalization.
- FixedAsset has original_cost and an acquisition journal, but no purchase invoice/line relationship. acquire_asset posts original_cost through the asset's configured counter account or treasury flow.

## Required additive data model

1. Preserve cost provenance across movements:
   - Each FIFO layer must retain a root receipt/source line and optional parent layer.
   - Transfers must create destination layers linked to the exact source layer quantities consumed; never replace the root receipt identity with the transfer line.
   - Record layer consumption against the issue/transfer movement line, quantity, and original layer. This is needed to split a later cost adjustment between remaining inventory and already-issued units.
2. Record a non-recoverable tax capitalization adjustment against the purchase invoice line and its receipt source line. The adjustment must be immutable once posted and idempotently linked to its journal entry.
3. Weighted-average products need a chronological cost-allocation ledger/replay mechanism. Do not approximate weighted-average allocation by simply changing StockBalance.average_cost or by distributing tax over current quantity: that loses the historical split between on-hand stock and COGS.
4. Fixed assets need an optional, protected source link to the purchase invoice line (or a dedicated asset-acquisition allocation record), with company, currency, amount, and duplicate-acquisition validations.

## Posting algorithm

For a purchase invoice line:
- Compute the non-recoverable tax portion separately from recoverable input VAT.
- Validate the tax code, company/country/date, invoice-to-receipt match, source movement is posted, and the accounting period for the adjustment is open.
- FIFO: use the root receipt lineage and layer-consumption history to allocate the adjustment proportionally to the receipt quantity still on hand versus quantity consumed. The on-hand share debits Inventory; the consumed share debits COGS. Transfer chains preserve the same root and branch/warehouse dimensions.
- Weighted average: replay the dated receipt/issue/transfer stream for the product and relevant warehouse/branch scope using the existing weighted-average rules, then split the adjustment at the invoice/adjustment effective date. If historical events cannot be reconstructed deterministically, reject posting and require a controlled correction workflow; never guess.
- Credit the appropriate GRNI/AP clearing or tax settlement account so the gross supplier liability remains correct. Recoverable tax alone debits VAT Input.
- Post a new adjustment journal in an open period. Never edit or reverse/rewrite an old posted journal as a side effect. If the original period is closed, use the current open period with a clearly referenced adjustment date/policy, or block pending an authorized accounting decision.
- Reconcile inventory subledger value to the inventory GL after posting.

## Fixed asset treatment

- Link the purchase line to one or more asset acquisition allocations where split acquisitions are valid; prevent the same tax amount being capitalized twice.
- Increase original_cost before the first depreciation schedule is posted, or use a separate approved cost-addition event with a prospective depreciation recalculation when depreciation has already begun.
- The acquisition journal must debit the asset account for net purchase cost plus non-recoverable tax, debit VAT Input only for recoverable tax, and credit the correct clearing/payable amount. Do not route supplier invoice liabilities through a cash/bank account.
- Existing posted depreciation and acquisition journals remain unchanged. Any later cost addition is a new journal and a controlled asset-cost event.

## Required tests before enabling the feature

- FIFO: untouched receipt, partial issue, full issue, multi-layer issue, transfer within a branch, inter-branch transfer, multiple transfers, partial return, expiry/location split, multiple warehouses.
- Weighted average: receipt/issue sequences, partial issue before invoice, transfers, returns, multiple warehouses/branches, and replay determinism.
- Tax: recoverable-only, non-recoverable-only, mixed tax lines, zero rate, invalid country/date, duplicate posting, closed period, and VAT return exclusion.
- GL: journal balances, Inventory subledger-to-GL reconciliation, COGS reconciliation, AP gross balance, and no historical journal mutation.
- Assets: acquisition before depreciation, acquisition with depreciation already started, split allocation, duplicate prevention, disposal after capitalized tax, and depreciation basis.
- Regression: all current purchasing, inventory, tax-report, and asset tests plus PostgreSQL CI.

## Rollout safeguards

- Add nullable/protected fields and append-only ledgers only; no destructive migration and no backfill that guesses provenance.
- Existing historical layers without lineage remain explicitly untraceable. The feature must refuse retroactive capitalization for those receipts until an audited opening/provenance correction is supplied.
- Keep the non-recoverable-tax validation guard enabled until FIFO and weighted-average allocation, asset linkage, journals, and reconciliation tests all pass.
- Enable by company/product only after reconciliation checks pass; preserve the current safe behavior as the default.
