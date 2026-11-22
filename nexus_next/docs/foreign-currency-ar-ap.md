# Foreign-currency receivable and payable settlement

## Current boundary

Sales invoices, purchase invoices, customer receipts, and supplier payments still
use the company's base currency. Treasury FX positions and revaluation do not
by themselves provide an AR/AP foreign-currency subledger. Do not enter foreign
invoice values into the existing base-currency amount fields or bypass the
payment-account currency checks.

## Settlement arithmetic foundation

`accounting.fx_settlement` contains pure Decimal helpers for:

- allocating the original base-currency carrying value to a partial settlement;
- assigning the exact remaining carrying value to the final settlement, avoiding
  rounding residuals;
- classifying realized FX gain/loss for receivables and payables.

The helpers do not create journal entries or persist allocations. They are not
a user-facing foreign-currency invoice feature and must not be wired into
posting until the document and allocation models are implemented.

## Required implementation before enabling foreign-currency AR/AP

1. Add immutable currency, foreign amount, approved rate, rate date, and base
   carrying-value snapshots to invoice headers and retain line-level foreign
   amounts and base valuations.
2. Add immutable receipt/payment allocation rows with foreign amount settled,
   settlement rate snapshot, allocated historical carrying value, and the
   resulting realized FX difference.
3. Lock invoice and allocation rows in one database transaction; prevent
   over-settlement across concurrent partial payments and preserve exact final
   residuals.
4. Post balanced journal entries using the historical AR/AP carrying value,
   actual base-currency cash/bank value, and configured realized FX gain/loss
   account roles. Keep the bank's currency and branch checks intact.
5. Add reversal/cancellation rules, statements showing both currencies, and
   tests for partial, final, same-rate, gain, loss, overpayment, concurrency,
   and period boundaries.
6. Only then expose foreign-currency invoice and settlement controls in React.

Existing posted invoices, payments, journal entries, and customer/supplier
balances must remain unchanged. No migration may rewrite historical amounts.
