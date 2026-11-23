"""Concurrent journal-draft creation and posting benchmark over authenticated HTTP."""
import atexit
import concurrent.futures
import http.cookiejar
import json
import math
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.db import connection

from accounts.models import User
from companies.models import Company, CompanyMembership, FiscalYear
from accounting.models import ChartOfAccount, JournalEntry, JournalLine

def require_isolated_benchmark_database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database_name = cursor.fetchone()[0]
    if not (
        database_name.endswith("benchmark")
        or database_name.endswith("nexus_test")
        or os.getenv("ALLOW_BENCHMARK_DATABASE") == "1"
    ):
        raise SystemExit(
            f"Refusing benchmark writes to database {database_name!r}; "
            "use an isolated *benchmark or nexus_test database."
        )


def deactivate_benchmark_users(usernames):
    try:
        for user in User.objects.filter(username__in=usernames):
            user.is_active = False
            user.set_unusable_password()
            user.save(update_fields=["is_active", "password"])
        CompanyMembership.objects.filter(user__username__in=usernames).update(is_active=False)
    except Exception:  # noqa: BLE001 - cleanup must not hide the benchmark's original result.
        pass


BASE = os.getenv("BENCHMARK_BASE_URL", "http://127.0.0.1:8000")
PASSWORD = secrets.token_urlsafe(32)
ENTRIES_PER_USER = 5
COUNTS = (10, 20, 30, 50)


def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def call_once(opener, url, method="GET", payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    started = time.perf_counter()
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, time.perf_counter() - started, response.read(), None
    except urllib.error.HTTPError as exc:
        return exc.code, time.perf_counter() - started, exc.read(), None
    except (TimeoutError, socket.timeout) as exc:
        return None, time.perf_counter() - started, b"", f"timeout: {exc}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return None, time.perf_counter() - started, b"", f"timeout: {exc.reason}"
        return None, time.perf_counter() - started, b"", f"network error: {exc.reason}"


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return 0
    return round(values[min(len(values) - 1, math.ceil(fraction * len(values)) - 1)] * 1000, 1)


def main():
    require_isolated_benchmark_database()
    company = Company.objects.filter(code__startswith="BENCH-").order_by("-id").first()
    if not company:
        raise SystemExit("The million-entry benchmark company was not found.")
    fiscal_year = FiscalYear.objects.filter(company=company, status=FiscalYear.Status.OPEN).first()
    branch = company.branches.filter(is_active=True).order_by("id").first()
    cash = ChartOfAccount.objects.filter(company=company, code="1110", is_active=True).first()
    sales = ChartOfAccount.objects.filter(company=company, code="4100", is_active=True).first()
    if not all((fiscal_year, branch, cash, sales)):
        raise SystemExit("Benchmark fiscal year, branch, or posting accounts are missing.")
    suffix = str(int(time.time()))
    user_records = []
    atexit.register(deactivate_benchmark_users, user_records)
    for index in range(max(COUNTS)):
        username = f"posting-load-{suffix}-{index + 1:02d}"
        user = User.objects.create_user(username=username, password=PASSWORD)
        CompanyMembership.objects.create(
            user=user, company=company, role=CompanyMembership.Role.ACCOUNTANT,
            all_branches=True,
        )
        user_records.append(username)

    report = {
        "company_id": company.pk,
        "fiscal_year_id": fiscal_year.pk,
        "entries_per_user": ENTRIES_PER_USER,
        "policy": "No automatic retry for POST requests to avoid duplicate financial writes.",
        "stages": [],
    }
    for count in COUNTS:
        barrier = threading.Barrier(count)
        lock = threading.Lock()
        create_samples, post_samples, errors = [], [], []
        prefix = f"POSTLOAD-{suffix}-{count}-"
        expected = count * ENTRIES_PER_USER

        def record_error(item, stage_lock=lock, stage_errors=errors):
            with stage_lock:
                stage_errors.append(item)

        def worker(username, stage_barrier=barrier, stage_lock=lock, stage_create_samples=create_samples, stage_post_samples=post_samples, stage_prefix=prefix):
            opener, jar = client()
            drafts = []
            try:
                status_code, _, _, error = call_once(opener, BASE + "/api/csrf/")
                csrf = next((cookie.value for cookie in jar if cookie.name == "csrftoken"), None)
                if status_code != 200 or not csrf:
                    raise RuntimeError(f"CSRF setup failed: HTTP {status_code}; {error or ''}")
                headers = {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrf,
                    "Referer": BASE + "/",
                }
                status_code, _, body, error = call_once(
                    opener, BASE + "/api/auth/login/", "POST",
                    {"username": username, "password": PASSWORD}, headers,
                )
                if status_code != 200:
                    raise RuntimeError(f"Login failed: HTTP {status_code}; {error or body[:300].decode('utf-8', 'replace')}")

                for iteration in range(ENTRIES_PER_USER):
                    entry_number = f"{stage_prefix}{username.rsplit('-', 1)[-1]}-{iteration + 1:02d}"
                    payload = {
                        "company": company.pk,
                        "branch": branch.pk,
                        "fiscal_year": fiscal_year.pk,
                        "entry_number": entry_number,
                        "entry_date": "2026-06-01",
                        "description": "Concurrent posting benchmark; synthetic data only",
                        "source_type": "manual",
                        "lines": [
                            {"account": cash.pk, "debit": "100.0000", "credit": "0"},
                            {"account": sales.pk, "debit": "0", "credit": "100.0000"},
                        ],
                    }
                    status_code, elapsed, body, error = call_once(
                        opener, BASE + "/api/accounting/journal-entries/",
                        "POST", payload, headers,
                    )
                    try:
                        response_data = json.loads(body.decode("utf-8")) if body else {}
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        response_data = {}
                    with stage_lock:
                        stage_create_samples.append(elapsed)
                    if status_code != 201 or not response_data.get("id"):
                        raise RuntimeError(
                            f"Draft creation failed: HTTP {status_code}; {error or body[:500].decode('utf-8', 'replace')}"
                        )
                    drafts.append((response_data["id"], entry_number))

                stage_barrier.wait(timeout=90)
                for entry_id, entry_number in drafts:
                    status_code, elapsed, body, error = call_once(
                        opener,
                        f"{BASE}/api/accounting/journal-entries/{entry_id}/post/",
                        "POST", {}, headers,
                    )
                    with stage_lock:
                        stage_post_samples.append(elapsed)
                    if status_code != 200:
                        record_error({
                            "user": username, "entry_number": entry_number,
                            "phase": "post", "status": status_code,
                            "error": error,
                            "body": body[:500].decode("utf-8", "replace"),
                        })
            except Exception as exc:  # noqa: BLE001 - capture each worker failure for the benchmark report.
                stage_barrier.abort()
                record_error({
                    "user": username,
                    "phase": "create_or_barrier",
                    "error": f"{type(exc).__name__}: {exc}",
                })

        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(worker, username) for username in user_records[:count]]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        elapsed = time.perf_counter() - started

        generated = JournalEntry.objects.filter(
            company=company, entry_number__startswith=prefix,
        )
        entry_count = generated.count()
        posted_count = generated.filter(status=JournalEntry.Status.POSTED).count()
        draft_count = generated.filter(status=JournalEntry.Status.DRAFT).count()
        totals = JournalLine.objects.filter(entry__in=generated).aggregate(
            debit=__import__("django.db.models", fromlist=["Sum"]).Sum("debit"),
            credit=__import__("django.db.models", fromlist=["Sum"]).Sum("credit"),
        )
        debit_total = totals["debit"] or 0
        credit_total = totals["credit"] or 0
        accounting_check = (
            entry_count == expected and posted_count == expected and
            draft_count == 0 and debit_total == credit_total and
            debit_total == expected * 100
        )
        stage = {
            "concurrent_users": count,
            "entries_expected": expected,
            "entries_created": entry_count,
            "entries_posted": posted_count,
            "drafts_left": draft_count,
            "debit_total": str(debit_total),
            "credit_total": str(credit_total),
            "accounting_check": accounting_check,
            "errors": len(errors),
            "wall_seconds": round(elapsed, 2),
            "create_latency_ms": {
                "samples": len(create_samples),
                "p50": percentile(create_samples, .50),
                "p95": percentile(create_samples, .95),
                "max": round(max(create_samples) * 1000, 1) if create_samples else 0,
            },
            "post_latency_ms": {
                "samples": len(post_samples),
                "p50": percentile(post_samples, .50),
                "p95": percentile(post_samples, .95),
                "max": round(max(post_samples) * 1000, 1) if post_samples else 0,
            },
            "error_samples": errors[:20],
        }
        report["stages"].append(stage)
        print(json.dumps(stage, ensure_ascii=False), flush=True)
        if errors or not accounting_check:
            report["failed"] = True

    output = Path("concurrent-posting-load-test.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"POSTING_LOAD_TEST_RESULT={output.resolve()}", flush=True)
    if report.get("failed") or len(report["stages"]) != len(COUNTS):
        raise SystemExit("Concurrent journal posting benchmark failed; see the JSON artifact.")


if __name__ == "__main__":
    main()
