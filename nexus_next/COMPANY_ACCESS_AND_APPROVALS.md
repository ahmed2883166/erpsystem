# NEXUS ERP — Company Access & Discount Approval

## Company roles

The company-scoped membership editor supports three predefined roles:

- **Company administrator:** manages company users, branch access, company setup and discount policies; can approve discount requests.
- **Accountant:** creates and processes accounting and sales documents but cannot change company memberships or discount policies.
- **Viewer:** read-only access.

Memberships can be assigned to all company branches or a selected branch set. Branches from another company are rejected. A company administrator must have company-wide branch scope. Non-admin users can see only their own membership; company administrators can manage memberships for their company. The last active company administrator cannot be disabled or demoted. Existing membership identity cannot be reassigned to another user; deactivate the old membership and create a new one instead.

Only active users can be found through the company-scoped user lookup. Search returns a limited set of account identifiers and display names, not user credentials or security data.

## Discount policy and approval

A company administrator configures a maximum line discount percentage in **الموافقات وحدود الخصم**.

- If no policy exists, no additional discount approval threshold is enforced.
- Once configured, the policy threshold is snapshotted onto each new sales invoice.
- Discounts at or below the threshold can be posted normally.
- Any invoice line discount above the snapshotted threshold automatically creates a pending approval request.
- The invoice remains a draft and posting is blocked until a different company administrator approves the request.
- The invoice creator cannot approve their own request, even if they are a company administrator.
- Rejection requires a reason. Approved/rejected decisions are final.
- Rejected draft invoices can be revised and resubmitted. Each attempt keeps its own immutable discount snapshot and decision; older pending attempts are marked superseded rather than deleted.
- Editing line values after approval invalidates that approval and creates a fresh request when the revised discount still exceeds the threshold. Editing notes alone does not invalidate the approval.
- Discount is calculated before VAT, so VAT is calculated on the discounted taxable base. The journal posts net sales after discount and the resulting VAT separately.

Changing the policy affects newly created invoices; it does not rewrite approval limits already captured on existing invoices.

## History and security

- Approval requests are read-only through the API and cannot be deleted. Multiple attempts can be attached to one invoice, preserving the full decision history.
- PostgreSQL guards approval identity fields, disallows deletion, and prevents changing a final decision.
- API mutations are captured by the existing audit middleware.
- Company membership changes, discount policy changes, and approval decisions are company-scoped and role-checked.
- Django Admin exposes these approval records as read-only; business decisions must use the audited API.

## Current role-model boundary

These are predefined roles, not yet a user-defined per-module permission matrix. The next access-control stage should add granular capabilities (view/create/edit/post/export/approve) by module, with least-privilege defaults and regression tests.

## Operational boundaries

The approval workflow currently covers **sales invoice line discounts**. It does not yet apply to purchase discounts, credit notes, quotations, or promotional pricing. It does not change HR/payroll, which remains out of scope.
