# Customer receipt batches

The sales API supports consolidated customer receipts in the company's base currency.

## Workflow

1. Create a draft at `/api/sales-documents/receipt-batches/` with a company, customer, branch, receipt number, date, method, amount, and optional `allocations`.
2. Each allocation contains an invoice ID and an amount. Every invoice must be posted, belong to the same customer/company and branch, be dated no later than the receipt, and use the company base currency.
3. Post the batch with `POST /api/sales-documents/receipt-batches/{id}/post/`. Posting creates one balanced cash/bank-to-receivables journal entry for the full receipt amount.
4. Any amount not allocated to invoices remains a customer advance. It is still credited to the customer receivables subledger and appears as an unallocated balance.
5. Allocate a posted advance later with `POST /api/sales-documents/receipt-batches/{id}/allocate/`, passing an invoice ID, amount, and allocation date. This changes only the customer subledger allocation; it does not create another cash movement or journal entry. The allocation date controls historical invoice aging, so a later allocation does not rewrite an earlier as-of report.
6. Reverse the whole batch with `POST /api/sales-documents/receipt-batches/{id}/reverse/`. The reversal creates a linked reversing journal and removes all of the batch's allocations from current invoice balances.

## Controls

- The sum of allocations cannot exceed the receipt amount or any invoice's open balance.
- A batch can allocate only to invoices for the same customer, company, branch, and base currency.
- Foreign-currency invoices continue to use the existing single-invoice receipt workflow so their carrying values and realized FX are calculated against the correct invoice.
- Posted batches and their allocation rows are immutable. A batch reversal reverses the entire receipt and all allocations together.
- The receipt statement and receivables reconciliation show the full batch amount once; invoice aging uses only the amount allocated to each invoice.
