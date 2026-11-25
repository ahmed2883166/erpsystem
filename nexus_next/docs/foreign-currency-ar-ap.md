# Foreign-currency receivable and payable settlement

## Implemented scope

The Django sales and purchasing workflows now support foreign-currency invoice
snapshots and single-invoice foreign-currency settlements:

- Sales and purchase invoice headers retain transaction currency, approved
  exchange-rate snapshot, rate date, and foreign totals. Invoice lines retain
  foreign unit prices and foreign totals while the GL remains in company base
  currency.
- Customer receipts and supplier payments retain the foreign amount, approved
  settlement rate/date, base-currency carrying value, and historical carrying
  value allocated to that settlement.
- Partial and final settlements allocate the remaining historical carrying
  value; final settlement absorbs the exact residual.
- Open foreign AR/AP balances can be revalued at an approved company exchange
  rate. Revaluation posts the unrealized FX gain/loss through configured account
  roles and records the prior carrying value, new carrying value, rate, foreign
  balance, and journal link.
- Customer and supplier invoice screens expose revaluation actions and history.
  The API also exposes invoice revaluation history.
- Settlement posting recognizes realized FX using historical carrying value and
  reverses the related unrealized amount. Revaluation and settlement dates are
  kept strictly ordered; same-day revaluation/settlement is rejected because
  documents currently store dates rather than intraday timestamps.
- Existing base-currency invoices and settlements retain their original amount
  semantics. Migrations add nullable historical carrying fields and do not
  rewrite old financial records.

## Current limitations

1. A receipt/payment is still linked to one invoice. A dedicated multi-invoice
   allocation model and consolidated receipt/payment allocation screen are not
   implemented yet.
2. AR/AP document settlement through foreign-currency bank or cash accounts is
   not enabled; document settlement accounts remain subject to the company's
   base-currency control. Treasury's separate foreign-currency position and
   revaluation workflow must not be confused with this limitation.
3. Customer and supplier statements show base-currency carrying values. A full
   dual-currency statement with original foreign amount, historical rate,
   settlement rate, and revaluation trail remains to be completed.
4. Project, customer, salesperson, and supplier analytical dimensions are not
   yet available on every journal line in the new Django accounting model.

## Accounting controls

- The invoice is locked during revaluation; approved exchange-rate snapshots
  must match company, currency, date, and rate.
- Only posted foreign-currency invoices with an open balance can be revalued.
- One revaluation per invoice per date is allowed.
- Revaluation cannot precede a posted settlement/revaluation or share a date
  with a settlement.
- Revaluation journals use separate unrealized gain/loss account roles.
- Settlement journals recognize realized gain/loss against historical carrying
  value and reverse the relevant unrealized balance. The journal remains in
  company base currency.
- Closed-period, company, branch, and account-role validation remain in force.

## Verification required before production approval

CI must pass on the exact latest branch commit, including migrations, backend
tests, frontend build, and Windows package jobs. Business acceptance should
then cover partial and final settlements, realized gains and losses, repeated
revaluations, over-settlement, date ordering, closed periods, statements, and
backup/restore on a dedicated Windows trial database.

Never test on or migrate the user's existing company database without a
verified backup and explicit approval.

## Currency precision policy

The accepted accounting policy is:

- Keep exchange rates and intermediate FX calculations at high Decimal precision; do not round an exchange rate or an intermediate multiplication merely for display.
- Use the currency's configured `decimal_places` as the minor-unit precision at the document-posting and presentation boundaries. EGP and SAR use two decimal places; other currencies follow their configured precision.
- Preserve historical posted amounts and balances exactly as stored. A policy change must not rewrite old journals, settlements, or opening balances.
- Round document components and totals deterministically before creating the final journal, then validate that the journal balances in the company base currency. Any rounding difference must be handled explicitly within the document/posting rules; it must never be hidden by weakening the journal-balance check.

**Implementation status:** the current accounting amount columns and FX conversion helpers still retain four decimal places. This is useful internal precision, but it is not yet the complete minor-unit posting policy above. Do not change only `to_base_amount` to two decimals: invoice line totals, tax, receipts/payments, settlement carrying values, and journal construction must adopt the same boundary together. Until that coordinated change is tested end-to-end, retain the existing four-decimal behavior and do not retroactively round historical balances.
