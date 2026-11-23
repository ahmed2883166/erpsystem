"""Capture EXPLAIN ANALYZE plans for the real accounting report API queries.

Run after seeding the isolated benchmark database. Bound values are used to execute
plans but are never written to the artifact.
"""
import json
import os
from pathlib import Path
import sys
from time import perf_counter

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.db import connection
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company, CompanyMembership
from accounting.models import ChartOfAccount


def capture_selects(client, url):
    captured = []

    def capture(execute, sql, params, many, context):
        normalized = sql.lstrip().upper()
        if normalized.startswith("SELECT") and "EXPLAIN" not in normalized[:80]:
            captured.append((sql, params))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(capture):
        # The benchmark runs with DEBUG=False and ALLOWED_HOSTS restricted to
        # localhost; APIClient's default "testserver" host otherwise returns 400.
        response = client.get(url, HTTP_HOST="localhost")
    if response.status_code != 200:
        raise RuntimeError(f"{url} returned HTTP {response.status_code}: {response.content[:500]!r}")
    if not captured:
        raise RuntimeError(f"{url} returned HTTP 200 but no SELECT statements were captured.")
    return captured


def main():
    company = Company.objects.filter(code__startswith="BENCH-").order_by("-id").first()
    account = ChartOfAccount.objects.filter(company=company, code="1110").first() if company else None
    if not company or not account:
        raise SystemExit("The million-entry benchmark company/account was not found.")

    username = "report-explain-diagnostics"
    user, _ = User.objects.get_or_create(username=username, defaults={"is_active": True})
    user.set_password("Explain-Only-Benchmark-2026!")
    user.save(update_fields=["password"])
    CompanyMembership.objects.update_or_create(
        user=user, company=company,
        defaults={"role": CompanyMembership.Role.ACCOUNTANT, "all_branches": True},
    )

    client = APIClient()
    client.force_authenticate(user=user)
    date_query = f"company={company.pk}&date_from=2026-01-01&date_to=2026-12-31"
    targets = {
        "trial_balance": f"/api/accounting/journal-entries/trial-balance/?{date_query}",
        "general_ledger": (
            f"/api/accounting/journal-entries/general-ledger/?{date_query}"
            f"&account={account.pk}&page=1&page_size=50"
        ),
    }

    report = {
        "dataset": "1,000,000 journal entries / 2,000,000 journal lines",
        "company_id": company.pk,
        "plans": {},
    }
    for name, url in targets.items():
        statements = capture_selects(client, url)
        seen = set()
        plans = []
        for sql, params in statements:
            signature = (sql, repr(params))
            if signature in seen:
                continue
            seen.add(signature)
            started = perf_counter()
            with connection.cursor() as cursor:
                cursor.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                    params,
                )
                plan = cursor.fetchone()[0]
            plans.append({
                "sql_shape": " ".join(sql.split())[:3000],
                "execution_ms": round((perf_counter() - started) * 1000, 2),
                "plan": plan,
            })
        report["plans"][name] = {
            "http_status": 200,
            "select_statements_captured": len(statements),
            "unique_select_plans": len(plans),
            "queries": plans,
        }

    output = Path("accounting-report-explain.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"EXPLAIN_ANALYZE_RESULT={output.resolve()}", flush=True)


if __name__ == "__main__":
    main()
