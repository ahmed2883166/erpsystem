# Treasury foreign-currency lifecycle

The treasury FX workflow keeps the general ledger in the company's functional/base currency and preserves the original foreign-currency amount and approved rate on each treasury document.

## Supported workflow

1. Create an active foreign-currency cash/bank account with **zero opening balance**. The currency must exist in the company's active currency reference data.
2. Enter an approved company exchange rate for the transaction date (or the selected approved rate date not later than the transaction date).
3. Create a receipt, payment, or same-currency transfer with the foreign nominal amount and approved rate. The application calculates the base-currency amount.
4. Posting updates the locked foreign-currency position and writes a balanced base-currency journal.
5. A foreign-currency payment removes the average carrying value from the bank position and recognizes the difference between the payment's spot value and carrying value using the configured realized FX gain/loss account roles.
6. Use **إعادة تقييم أرصدة العملات الأجنبية** at period end. The endpoint requires an open fiscal year, an approved rate dated no later than the revaluation date, and posts the unrealized FX gain/loss to the configured account role. The account/date pair cannot be revalued twice.
7. Foreign bank reconciliation uses the bank statement's nominal currency amount. The general ledger remains measured in base currency; revaluation entries do not change the nominal bank balance.

## Required account-role mappings

Configure these roles in the company's account-role settings before posting the related operation:

- `realized_fx_gain` — revenue account
- `realized_fx_loss` — expense account
- `unrealized_fx_gain` — revenue account
- `unrealized_fx_loss` — expense account

Only the role needed for the direction of a particular difference is required at that time.

## Opening balances

A foreign-currency treasury account may be created with an opening nominal amount, an approved company exchange rate and its rate date. The base-currency carrying amount is stored in `TreasuryAccount.opening_balance`; the nominal amount and rate evidence are stored separately. The opening journal debits the treasury GL account in functional currency and retains the foreign-currency snapshot on that journal line, with retained earnings as the opening counterpart. The same transaction initializes `TreasuryFxPosition` with the nominal foreign amount and base-currency carrying value.

The rate must be an exact active company rate dated no later than the opening journal date. The opening balance is immutable after account creation. Legacy foreign accounts with opening values but no complete FX snapshot, posted opening journal and matching FX position remain blocked; do not clear or rewrite their values to bypass the safeguard. Reconcile them through an approved opening conversion before enabling FX activity.

The system also refuses to initialize a new FX position if posted foreign treasury history exists without a position record. This prevents silently rebuilding a position from incomplete history.

## Current scope boundary

This release covers foreign-currency treasury receipts, payments, same-currency transfers, average-cost carrying value, realized differences, period-end revaluation, and nominal foreign bank reconciliation. It does **not** yet implement foreign-currency customer/supplier invoices, receivable/payable partial settlements, customer/supplier advance allocation, FX settlement across different currencies, or consolidated multi-currency exposure reporting. Those workflows remain disabled or base-currency-only until their document-level carrying-value and settlement rules are implemented and tested.