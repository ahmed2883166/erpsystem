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
