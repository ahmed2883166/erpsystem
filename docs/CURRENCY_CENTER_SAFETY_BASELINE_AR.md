# Currency center safety baseline — before PostgreSQL conversion

This baseline adds regression tests only. It does not change currency-center behavior or database selection.

## Covered behavior
- Reject invalid and non-finite currency amounts.
- Preserve two-decimal ROUND_HALF_UP money rounding.
- Keep a partially settled foreign-currency transaction open.
- Mark a fully settled transaction as settled.
- Reject a stale expected balance rather than overwriting a newer balance.

## Deliberate limits
- Tests use an in-memory SQLite database and never open the company database.
- These tests do not claim PostgreSQL compatibility.
- Settlement/revaluation journal flows, transaction locking, and PostgreSQL row-level locking remain to be covered before the currency center can be called PostgreSQL-ready.
