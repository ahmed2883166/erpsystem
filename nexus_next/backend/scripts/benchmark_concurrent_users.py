"""Concurrent authenticated HTTP benchmark against an isolated million-entry database."""
import concurrent.futures
import http.cookiejar
import json
import math
import os
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

def call(opener, url, method="GET", payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    start = time.perf_counter()
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, time.perf_counter() - start, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, time.perf_counter() - start, exc.read()

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
    for i in range(30):
        username = f"loadtest-{suffix}-{i+1:02d}"
        user = User.objects.create_user(username=username, password=PASSWORD)
        CompanyMembership.objects.create(user=user, company=company,
            role=CompanyMembership.Role.ACCOUNTANT, all_branches=True)
        usernames.append(username)

    date_query = f"company={company.pk}&date_from=2026-01-01&date_to=2026-12-31"
    endpoints = [
        ("journal_list", f"/api/accounting/journal-entries/?company={company.pk}&page=1&page_size=100"),
        ("journal_summary", f"/api/accounting/journal-entries/summary/?company={company.pk}"),
        ("trial_balance", f"/api/accounting/journal-entries/trial-balance/?{date_query}"),
        ("general_ledger", f"/api/accounting/journal-entries/general-ledger/?{date_query}&account={account.pk}&page=1&page_size=100"),
    ]
    report = {"company_id": company.pk, "dataset": "1,000,000 journal entries / 2,000,000 lines",
              "iterations_per_user": ITERATIONS, "stages": []}

    for count in (10, 20, 30):
        barrier = threading.Barrier(count)
        samples, errors = [], []
        lock = threading.Lock()
        start_stage = time.perf_counter()

        def worker(username, stage_errors=errors, stage_samples=samples, stage_lock=lock, stage_barrier=barrier):
            opener, jar = client()
            code, _, _ = call(opener, BASE + "/api/csrf/")
            csrf = next((c.value for c in jar if c.name == "csrftoken"), None)
            if code != 200 or not csrf:
                raise RuntimeError(f"CSRF setup failed for {username}: HTTP {code}")
            code, _, body = call(opener, BASE + "/api/auth/login/", "POST",
                {"username": username, "password": PASSWORD},
                {"Content-Type": "application/json", "X-CSRFToken": csrf, "Referer": BASE + "/"})
            if code != 200:
                raise RuntimeError(f"Login failed for {username}: HTTP {code} {body[:200]!r}")
            stage_barrier.wait(timeout=30)
            own = []
            for _ in range(ITERATIONS):
                for name, path in endpoints:
                    status, duration, body = call(opener, BASE + path)
                    own.append((name, status, duration))
                    if status != 200:
                        with stage_lock:
                            stage_errors.append({"user": username, "endpoint": name, "status": status,
                                           "body": body[:200].decode("utf-8", "replace")})
            with stage_lock:
                stage_samples.extend(own)

        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(worker, name) for name in usernames[:count]]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except (RuntimeError, TimeoutError, threading.BrokenBarrierError, OSError) as exc:
                    errors.append({"error": str(exc)})

        elapsed = time.perf_counter() - start_stage
        by_name = {}
        for name, status, duration in samples:
            by_name.setdefault(name, []).append(duration)
        stats = {name: {"requests": len(vals), "p50_ms": pct(vals, .50),
                        "p95_ms": pct(vals, .95), "p99_ms": pct(vals, .99),
                        "max_ms": round(max(vals)*1000, 1)}
                 for name, vals in by_name.items()}
        stage = {"concurrent_users": count, "requests_expected": count*ITERATIONS*len(endpoints),
                 "requests_completed": len(samples), "errors": len(errors),
                 "wall_seconds": round(elapsed, 2),
                 "throughput_requests_per_second": round(len(samples)/elapsed, 2) if elapsed else 0,
                 "endpoints": stats, "error_samples": errors[:10]}
        report["stages"].append(stage)
        print(json.dumps(stage, ensure_ascii=False), flush=True)
        if errors:
            report["failed"] = True
            break

    output = Path("concurrent-load-test.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"LOAD_TEST_RESULT={output.resolve()}", flush=True)
    if report.get("failed") or len(report["stages"]) != 3:
        raise SystemExit("Concurrent load test failed; see the JSON artifact.")

if __name__ == "__main__":
    main()
