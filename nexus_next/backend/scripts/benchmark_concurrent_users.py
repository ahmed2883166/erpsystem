"""Concurrent authenticated HTTP benchmark against an isolated million-entry database."""
import concurrent.futures
import http.cookiejar
import json
import math
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import sys

# When executed as ``python scripts/...py``, Python puts the scripts folder
# (not the Django project root) at sys.path[0]. Add the backend root so that
# ``config.settings`` and the Django apps resolve exactly as they do via manage.py.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from accounts.models import User
from companies.models import Company, CompanyMembership
from accounting.models import ChartOfAccount

BASE = os.getenv("BENCHMARK_BASE_URL", "http://127.0.0.1:8000")
PASSWORD = "Concurrent-Benchmark-Only-2026!"
ITERATIONS = 5


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


def call(opener, url, method="GET", payload=None, headers=None):
    """Retry a timed-out request exactly once; never retry a completed HTTP response."""
    total_duration = 0.0
    last = (None, 0.0, b"", "request was not attempted")
    for attempt in range(2):
        last = call_once(opener, url, method, payload, headers)
        total_duration += last[1]
        if last[3] is None or not last[3].startswith("timeout:") or attempt == 1:
            break
    return last[0], total_duration, last[2], last[3]


def pct(values, fraction):
    values = sorted(values)
    return round(values[min(len(values)-1, math.ceil(fraction*len(values))-1)]*1000, 1) if values else 0


def main():
    company = Company.objects.filter(code__startswith="BENCH-").order_by("-id").first()
    account = ChartOfAccount.objects.filter(company=company, code="1110").first() if company else None
    if not company or not account:
        raise SystemExit("The million-entry benchmark company/account was not found.")
    suffix = str(int(time.time()))
    usernames = []
    for i in range(50):
        username = f"loadtest-{suffix}-{i+1:02d}"
        user = User.objects.create_user(username=username, password=PASSWORD)
        CompanyMembership.objects.create(
            user=user, company=company,
            role=CompanyMembership.Role.ACCOUNTANT, all_branches=True,
        )
        usernames.append(username)

    date_query = f"company={company.pk}&date_from=2026-01-01&date_to=2026-12-31"
    endpoints = [
        ("journal_list", f"/api/accounting/journal-entries/?company={company.pk}&page=1&page_size=100"),
        ("journal_summary", f"/api/accounting/journal-entries/summary/?company={company.pk}"),
        ("trial_balance", f"/api/accounting/journal-entries/trial-balance/?{date_query}"),
        ("general_ledger", f"/api/accounting/journal-entries/general-ledger/?{date_query}&account={account.pk}&page=1&page_size=100"),
    ]
    requests_per_user = ITERATIONS * len(endpoints)
    report = {
        "company_id": company.pk,
        "dataset": "1,000,000 journal entries / 2,000,000 journal lines",
        "iterations_per_user": ITERATIONS,
        "retry_policy": "one retry for timeout only",
        "stages": [],
    }

    for count in (10, 20, 30, 50):
        barrier = threading.Barrier(count)
        samples, errors = [], []
        lock = threading.Lock()
        start_stage = time.perf_counter()

        def add_error(record, error_lock=lock, stage_errors=errors):
            with error_lock:
                stage_errors.append(record)

        def worker(username, stage_barrier=barrier, sample_lock=lock, stage_samples=samples):
            attempted = 0
            opener, jar = client()
            try:
                code, _, _, error = call(opener, BASE + "/api/csrf/")
                csrf = next((cookie.value for cookie in jar if cookie.name == "csrftoken"), None)
                if code != 200 or not csrf:
                    raise RuntimeError(f"CSRF setup failed: HTTP {code}; {error or ''}")
                code, _, body, error = call(
                    opener, BASE + "/api/auth/login/", "POST",
                    {"username": username, "password": PASSWORD},
                    {"Content-Type": "application/json", "X-CSRFToken": csrf, "Referer": BASE + "/"},
                )
                if code != 200:
                    raise RuntimeError(
                        f"Login failed: HTTP {code}; {error or body[:200].decode('utf-8', 'replace')}"
                    )
                stage_barrier.wait(timeout=60)

                for iteration in range(ITERATIONS):
                    for name, path in endpoints:
                        attempted += 1
                        status_code, duration, body, error = call(opener, BASE + path)
                        with sample_lock:
                            stage_samples.append((name, status_code, duration))
                        if status_code != 200:
                            add_error({
                                "user": username, "iteration": iteration + 1,
                                "endpoint": name, "status": status_code,
                                "error": error,
                                "body": body[:200].decode("utf-8", "replace"),
                            })
            except Exception as exc:  # noqa: BLE001 - worker boundary records every failure as request errors.
                # Account for every request this worker did not get to issue.
                remaining = requests_per_user - attempted
                for missing in range(remaining):
                    name = endpoints[(attempted + missing) % len(endpoints)][0]
                    add_error({
                        "user": username, "endpoint": name,
                        "error": f"worker stopped before request: {type(exc).__name__}: {exc}",
                        "not_sent": True,
                    })

        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(worker, username) for username in usernames[:count]]
            # Workers capture their own failures and convert unsent requests into
            # request-level errors; collecting every future ensures no silent loss.
            for future in concurrent.futures.as_completed(futures):
                future.result()

        elapsed = time.perf_counter() - start_stage
        by_name = {}
        for name, status_code, duration in samples:
            by_name.setdefault(name, []).append(duration)
        stats = {
            name: {
                "responses": len(values),
                "p50_ms": pct(values, .50),
                "p95_ms": pct(values, .95),
                "p99_ms": pct(values, .99),
                "max_ms": round(max(values) * 1000, 1),
            }
            for name, values in by_name.items()
        }
        expected = count * requests_per_user
        completed = sum(1 for _, status_code, _ in samples if status_code == 200)
        errored = len(errors)
        stage = {
            "concurrent_users": count,
            "requests_expected": expected,
            "requests_completed": completed,
            "requests_errored": errored,
            "accounting_check": completed + errored == expected,
            "http_responses_received": len(samples),
            "wall_seconds": round(elapsed, 2),
            "throughput_successful_requests_per_second": round(completed / elapsed, 2) if elapsed else 0,
            "endpoints": stats,
            "error_samples": errors[:20],
        }
        report["stages"].append(stage)
        print(json.dumps(stage, ensure_ascii=False), flush=True)
        if not stage["accounting_check"] or errored:
            report["failed"] = True

    output = Path("concurrent-load-test.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"LOAD_TEST_RESULT={output.resolve()}", flush=True)
    if report.get("failed") or len(report["stages"]) != 4:
        raise SystemExit("Concurrent load test failed; see the JSON artifact.")


if __name__ == "__main__":
    main()
