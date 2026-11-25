"""Isolated PostgreSQL benchmark for one million posted journal entries."""
import atexit, argparse, json, os, resource, secrets, time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

import psutil
from django.db import connection
from rest_framework.test import APIClient
from accounts.models import User
from companies.models import Branch, Company, CompanyMembership, Currency, FiscalYear
from accounting.models import ChartOfAccount, JournalEntry, JournalLine

def deactivate_benchmark_user(username):
    try:
        user = User.objects.filter(username=username).first()
        if user:
            user.is_active = False
            user.set_unusable_password()
            user.save(update_fields=["is_active", "password"])
            CompanyMembership.objects.filter(user=user).update(is_active=False)
    except Exception:  # noqa: BLE001 - cleanup must not hide the benchmark's original result.
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--entries", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=5_000)
    args = parser.parse_args()
    if args.entries != 1_000_000:
        raise SystemExit("This benchmark is fixed at exactly 1,000,000 entries.")
    if connection.vendor != "postgresql":
        raise SystemExit("PostgreSQL is required.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        db_name = cursor.fetchone()[0]
    if not (db_name.endswith("benchmark") or db_name.endswith("nexus_test")):
        raise SystemExit(f"Refusing to write synthetic data to database {db_name!r}.")
    metrics = {"database": db_name, "target_journal_entries": args.entries, "lines_per_entry": 2,
               "batch_size": args.batch_size, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    process = psutil.Process()
    metrics["rss_before_mb"] = round(process.memory_info().rss / 1024 / 1024, 1)
    currency, _ = Currency.objects.get_or_create(code="EGP", defaults={"name_ar": "جنيه مصري"})
    suffix = str(int(time.time()))[-8:]
    company = Company.objects.create(code=f"BENCH-{suffix}", name_ar="شركة اختبار مليون قيد",
        country=Company.Country.EGYPT, activity=Company.Activity.COMMERCIAL, base_currency=currency)
    branch = Branch.objects.create(company=company, code="MAIN", name_ar="الفرع الرئيسي", is_head_office=True)
    fiscal_year = FiscalYear.objects.create(company=company, name="2026",
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
    cash = ChartOfAccount.objects.create(company=company, code="1110", name_ar="الصندوق", nature=ChartOfAccount.Nature.ASSET)
    sales = ChartOfAccount.objects.create(company=company, code="4100", name_ar="المبيعات", nature=ChartOfAccount.Nature.REVENUE)
    user = User.objects.create_user(username=f"million-benchmark-{suffix}", password=secrets.token_urlsafe(32))
    atexit.register(deactivate_benchmark_user, user.username)
    CompanyMembership.objects.create(user=user, company=company, role=CompanyMembership.Role.ACCOUNTANT, all_branches=True)

    started = time.perf_counter()
    for first in range(1, args.entries + 1, args.batch_size):
        last = min(first + args.batch_size, args.entries + 1)
        entries = [JournalEntry(company=company, branch=branch, fiscal_year=fiscal_year,
            entry_number=f"BENCH-{number:07d}",
            entry_date=date(2026, 1, 1) + timedelta(days=(number - 1) % 365),
            description="Synthetic balanced benchmark journal", source_type=JournalEntry.Source.MANUAL,
            status=JournalEntry.Status.POSTED) for number in range(first, last)]
        JournalEntry.objects.bulk_create(entries, batch_size=args.batch_size)
        lines = []
        for number, entry in zip(range(first, last), entries):
            amount = 100 + (number % 17)
            lines.extend((
                JournalLine(entry=entry, account=cash, description="Synthetic debit", debit=amount, credit=0),
                JournalLine(entry=entry, account=sales, description="Synthetic credit", debit=0, credit=amount),
            ))
        JournalLine.objects.bulk_create(lines, batch_size=min(args.batch_size * 2, 5_000))
        if first == 1 or (first - 1) % 100_000 == 0:
            print(f"Inserted {last - 1:,}/{args.entries:,} journal entries", flush=True)
    metrics["bulk_load_seconds"] = round(time.perf_counter() - started, 3)
    metrics["journal_entry_count"] = JournalEntry.objects.filter(company=company).count()
    metrics["journal_line_count"] = JournalLine.objects.filter(entry__company=company).count()
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(SUM(debit),0), COALESCE(SUM(credit),0) FROM accounting_journalline jl JOIN accounting_journalentry je ON je.id=jl.entry_id WHERE je.company_id=%s", [company.pk])
        debit_total, credit_total = cursor.fetchone()
        cursor.execute("SELECT pg_database_size(current_database())")
        metrics["database_size_mb"] = round(cursor.fetchone()[0] / 1024 / 1024, 1)
    metrics["debit_total"], metrics["credit_total"] = str(debit_total), str(credit_total)
    metrics["balanced"] = debit_total == credit_total

    client = APIClient()
    # Django DEBUG=False rejects the test client default host "testserver" when it is not in ALLOWED_HOSTS.
    # Use the same loopback host allowed by the local Windows deployment settings.
    client.defaults["HTTP_HOST"] = "127.0.0.1"
    client.force_authenticate(user=user)
    query = f"company={company.pk}&date_from=2026-01-01&date_to=2026-12-31"
    def request_metric(name, url, row_key=None):
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as captured:
            started_at = time.perf_counter()
            response = client.get(url)
            elapsed = time.perf_counter() - started_at
        result = {"status_code": response.status_code, "seconds": round(elapsed, 3),
            "query_count": len(captured), "response_mb": round(len(response.content) / 1024 / 1024, 2)}
        if response.status_code != 200:
            result["body_preview"] = response.content[:1000].decode("utf-8", "replace")
            metrics[name] = result
            print(f"{name} ERROR BODY: {result['body_preview']}", flush=True)
            output = Path(os.getenv("BENCHMARK_OUTPUT", "million-journal-benchmark.json"))
            output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"{name} returned HTTP {response.status_code}: {result['body_preview']}")
        data = response.data
        if row_key:
            rows = data.get(row_key, []) if isinstance(data, dict) else data
            result["rows_returned"] = len(rows)
        if name == "journal_list":
            result["page_count"] = data.get("count") if isinstance(data, dict) else None
            result["page_rows"] = len(data.get("results", [])) if isinstance(data, dict) else None
        if name == "journal_summary":
            result["summary"] = data
        if name == "trial_balance":
            result["keys"] = sorted(data.keys()) if isinstance(data, dict) else []
        if name.startswith("general_ledger"):
            result["pagination"] = data.get("pagination", {}) if isinstance(data, dict) else {}
        metrics[name] = result
        print(f"{name}: {json.dumps(result, ensure_ascii=False)}", flush=True)
        return data
    request_metric("journal_list", f"/api/accounting/journal-entries/?company={company.pk}&page=1&page_size=100")
    request_metric("journal_summary", f"/api/accounting/journal-entries/summary/?company={company.pk}")
    request_metric("trial_balance", f"/api/accounting/journal-entries/trial-balance/?{query}")
    ledger_page_1 = request_metric(
        "general_ledger",
        f"/api/accounting/journal-entries/general-ledger/?{query}&account={cash.pk}&page=1&page_size=100",
        row_key="rows",
    )
    ledger_page_2 = request_metric(
        "general_ledger_page_2",
        f"/api/accounting/journal-entries/general-ledger/?{query}&account={cash.pk}&page=2&page_size=100",
        row_key="rows",
    )
    if ledger_page_1.get("pagination", {}).get("total_rows") != args.entries:
        raise RuntimeError("General ledger pagination total does not match the million journal-line target.")
    first_page_rows = ledger_page_1.get("rows", [])
    second_page_rows = ledger_page_2.get("rows", [])
    if len(first_page_rows) != 100 or len(second_page_rows) != 100:
        raise RuntimeError("General ledger page size is not respected.")
    if first_page_rows and second_page_rows:
        expected_next_balance = (
            Decimal(str(first_page_rows[-1]["running_balance"]))
            + Decimal(str(second_page_rows[0]["debit"]))
            - Decimal(str(second_page_rows[0]["credit"]))
        )
        if expected_next_balance != Decimal(str(second_page_rows[0]["running_balance"])):
            raise RuntimeError("General ledger running balance is discontinuous between pages.")
    metrics["general_ledger_pagination_continuity"] = True
    metrics["rss_after_mb"] = round(process.memory_info().rss / 1024 / 1024, 1)
    metrics["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    if metrics["journal_entry_count"] != args.entries or metrics["journal_line_count"] != args.entries * 2 or not metrics["balanced"]:
        raise RuntimeError("Row counts or debit/credit reconciliation failed.")
    output = Path(os.getenv("BENCHMARK_OUTPUT", "million-journal-benchmark.json"))
    output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"RESULT_JSON={output.resolve()}", flush=True)
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)

if __name__ == "__main__":
    main()
