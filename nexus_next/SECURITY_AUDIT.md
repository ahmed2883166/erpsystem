# Admrail ERP v2 ERP — Security & Audit Review

**Review date:** 2026-11-21  
**Reviewed branch:** `work/nexus-django-react-rebuild`  
**Baseline reviewed commit:** `f36bc6516da0ea7516db0f60c0f06e90c9ad445b`  
**CI evidence:** [GitHub Actions run 44936483695](https://github.com/ahmed2883166/erpsystem/actions/runs/44936483695)

## Scope

This pass reviewed the Django/DRF API security boundary, company and branch authorization, treasury mutation paths, the audit-event pipeline, PostgreSQL migrations, and frontend integration of the audit screen. No legacy application files or company data were changed. CI uses an isolated PostgreSQL 16 service database.

## Controls implemented in this pass

1. **Append-only audit events**
   - Records API write attempts (POST, PUT, PATCH, DELETE), including rejected requests.
   - Captures actor, company, branch, request ID, endpoint, object identifier, HTTP method, response status, timestamp, IP address, and user agent.
   - Does not persist request bodies, response bodies, passwords, tokens, or invoice payloads.
   - Application model/queryset rejects update and delete operations.
   - PostgreSQL trigger rejects direct SQL UPDATE and DELETE.
   - Actor/company/branch references use PROTECT to preserve audit identity and ownership history.
   - Audit API is read-only and filters company/branch visibility by membership role.
   - Frontend audit screen supports company, date-range, and action filters.

2. **Company setup authorization**
   - Company, branch, and fiscal-year modifications now require the company administrator role.
   - Company creation remains restricted to system staff; the creator receives the company-admin membership.
   - Currency administration remains system-staff-only.
   - Deleting a company or branch with audit history is blocked with a clear instruction to deactivate it instead.

3. **Treasury authorization and integrity**
   - Treasury account creation/update checks access to the selected branch.
   - Treasury accounts and transactions cannot be moved between companies after creation.
   - Draft transaction branch reassignment is checked against the user's branch membership.
   - Posted treasury transactions cannot be deleted through the API.
   - Bank reconciliation updates validate company and bank-branch scope.

4. **Security configuration baseline**
   - Django secret key and PostgreSQL password are required environment settings; no unsafe application fallback is supplied.
   - DEBUG defaults to false.
   - Password validation includes a 12-character minimum and Django's similarity/common/numeric validators.
   - Session cookies are HttpOnly and SameSite=Lax; CSRF and content-type protections are enabled.

## Verified test evidence

At baseline commit `f36bc6516da0ea7516db0f60c0f06e90c9ad445b`:

- Django system check: passed.
- `makemigrations --check --dry-run`: passed.
- PostgreSQL migrations: passed, including the append-only audit trigger and protected references.
- Backend test suite: **134 tests passed**.
- Frontend TypeScript typecheck: passed.
- Frontend production build: passed.
- GitHub Actions conclusion: **success** for backend and frontend jobs.

## Important limitations — not yet a security certification

- Audit capture currently covers API write requests under `/api/`; direct Django Admin mutations are not yet included in this audit stream.
- Audit insertion is performed by middleware after the response. It is append-only once stored, but it is not yet transactionally coupled to every business mutation. If audit persistence fails, the middleware logs the failure and does not replace the business response.
- A database owner/superuser with permission to alter triggers or schema can bypass database-level immutability. Production database roles and operational controls must restrict that access.
- HTTPS/TLS deployment for private-LAN access, login throttling/account lockout, MFA, session expiry policy, backup/restore drills, and external penetration testing remain separate hardening work.
- No claim is made that the application has undergone a complete financial-statement or independent penetration audit.

## Next review priorities

1. Extend audit capture to Django Admin and authentication/security events.
2. Make critical financial audit events transactionally consistent with posting/reversal operations.
3. Finish role-management and approval workflows with least-privilege defaults.
4. Review historical visibility for inactive branches and company-wide consolidated reports.
5. Test production backup/restore, TLS-enabled private-LAN deployment, concurrent users, and large-ledger performance.

## Follow-up controls added — 2026-11-22

### HR segregation of duties
- Leave requests now persist the creating user. The creator cannot approve or reject their own request.
- A payroll-cycle creator cannot approve that cycle.
- A narrowly scoped emergency override is available only to a Django superuser and requires a written reason of at least 10 characters. The override is written to the append-only audit stream with the target, decision, actor, and reason.
- Both approval paths lock the target row during the decision to reduce concurrent double-decision races.
- Existing leave rows have a nullable creator because historical records predate this field; new API-created requests always set it server-side.

### Superuser global visibility
- A Django superuser is intentionally a platform-wide operator: company and branch access helpers return all active companies and branches, except for a separately designated platform owner account which has no company operating access.
- Every authenticated superuser API or Django Admin GET request is now recorded as an audit action with metadata identifying the superuser API read and the global company/branch visibility policy. This is intentionally broad and can increase audit volume.
- Keep superuser accounts to a very small named set (recommended maximum: two), protect them with unique strong credentials, and use ordinary company roles for daily work. Review the superuser audit stream regularly.

### Production HTTPS gate
- The production launcher now refuses to run before database migration unless secure session cookies, secure CSRF cookies, HTTPS redirect, a one-year HSTS policy, HTTPS CSRF trusted origins, and a trusted HTTPS-terminating proxy are configured.
- Django only trusts the forwarded-proto header when DJANGO_HTTPS_TERMINATED_BY_PROXY=True. The reverse proxy must remove any client-supplied forwarded-proto header and set it itself.
- The application server is not itself a TLS server. Production and LAN deployments must place a correctly configured TLS reverse proxy in front of Waitress; do not expose the raw Waitress HTTP listener to untrusted networks.
- The Django deployment check remains a mandatory startup gate.

### Base-currency journal precision
- Before a journal can be posted, each debit and credit line is rounded using ROUND_HALF_UP to the configured base currency decimal_places.
- A line that rounds to zero on both sides is rejected, and the journal is rejected if the rounded totals do not balance.
- FX snapshot validation compares the base equivalent at the company's configured base-currency precision. Stored journal columns retain four decimal places for schema compatibility, but posted general-ledger debit/credit amounts are rounded to two decimals. Invoice, receipt, payment, treasury-position, and inventory subledger fields still retain four-decimal precision; end-to-end subledger-to-GL reconciliation at the base-currency minor unit remains a required acceptance check.
