# Inventory NRV Assessment — IAS 2

## Measurement rule

The user enters the **net realizable value (NRV) per unit** for one stock item in one warehouse and an assessment date. NRV means the estimated selling price in the ordinary course of business less estimated costs of completion and costs necessary to make the sale. Those estimates remain an accounting judgment and must be supported by the company's evidence; the application does not infer market prices.

The system compares NRV with the original cost basis of the remaining stock and carries the stock at the lower amount:
- FIFO: assess each remaining cost layer against the entered per-unit NRV.
- Weighted average: apply the lower of the original weighted-average cost and NRV to the remaining balance.
- Reassessments can recognize a partial reversal when NRV improves, but never above the original cost basis.

## Posting and controls

- Assessment is scoped to one company, product, warehouse, and date.
- The system requires complete, matching stock-cost layers and stock-balance values before posting.
- Draft stock movements and movements dated after the assessment date block posting.
- The posting debits the configured inventory-adjustment-loss account and credits inventory for a write-down; a recovery reverses those sides.
- The journal uses the warehouse's branch and the assessment date's open fiscal period.
- Reversal restores the exact pre-assessment balance and layer costs and posts a linked journal reversal.
- Reversal is blocked after a later stock movement or a later posted NRV assessment. The accountant must instead make a new assessment using the current stock state.

## Acceptance coverage

The inventory API tests cover a FIFO write-down, partial recovery after an NRV increase, full reversal of assessments in reverse order, blocking reversal after a later stock movement, and weighted-average reassessment after a subsequent receipt. The test suite also checks migrations and the normal accounting posting validations.
