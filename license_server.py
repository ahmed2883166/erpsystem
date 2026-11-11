"""Standalone HTTPS licensing authority for NEXUS ERP deployments."""
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

app = Flask(__name__)

def utc_now():
    return datetime.now(timezone.utc)

def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

def db_path():
    return Path(os.environ.get("NEXUS_LICENSE_DB", "nexus-licenses.sqlite3"))

def connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS licenses(
        id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, customer TEXT NOT NULL,
        license_type TEXT NOT NULL, expires_at TEXT NOT NULL, device_id TEXT,
        revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS trial_devices(
        machine_id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, trial_ends_at TEXT NOT NULL
    )""")
    connection.commit()
    return connection

def private_key():
    raw = os.environ.get("NEXUS_LICENSE_PRIVATE_KEY", "")
    if not raw:
        raise RuntimeError("NEXUS_LICENSE_PRIVATE_KEY is not configured")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))

def encode_token(payload):
    payload_part = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
    signature = private_key().sign(payload_part.encode("ascii"))
    return payload_part + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

def decode_token(token, machine_id):
    try:
        parts = (token or "").split(".")
        if len(parts) != 2:
            return None
        raw = os.environ.get("NEXUS_LICENSE_PRIVATE_KEY", "")
        if not raw:
            return None
        public = private_key().public_key()
        signature = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        public.verify(signature, parts[0].encode("ascii"))
        payload_bytes = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
        payload = json.loads(payload_bytes.decode("utf-8"))
        if payload.get("machine_id") != machine_id or parse_time(payload["expires_at"]) <= utc_now():
            return None
        return payload
    except Exception:
        return None

def make_token(license_row, machine_id):
    now = utc_now()
    expiry = parse_time(license_row["expires_at"])
    payload = {
        "license_id": license_row["id"],
        "machine_id": machine_id,
        "customer": license_row["customer"],
        "license_type": license_row["license_type"],
        "issued_at": iso(now),
        "expires_at": iso(expiry),
        "offline_until": iso(min(expiry, now + timedelta(days=7)))
    }
    return encode_token(payload)

def make_trial_token(machine_id, first_seen, trial_ends_at):
    payload = {
        "license_id": "trial:" + hashlib.sha256(machine_id.encode()).hexdigest(),
        "machine_id": machine_id,
        "customer": "",
        "license_type": "trial",
        "issued_at": iso(first_seen),
        "expires_at": iso(trial_ends_at),
        "offline_until": iso(trial_ends_at),
        "trial_ends_at": iso(trial_ends_at),
    }
    return encode_token(payload)


def admin_authorized():
    expected = os.environ.get("NEXUS_LICENSE_ADMIN_TOKEN", "")
    supplied = request.headers.get("Authorization", "")
    return bool(expected and secrets.compare_digest(supplied, "Bearer " + expected))

@app.get("/health")
def health():
    return jsonify({"service":"NEXUS ERP Licensing","status":"ok"})

@app.post("/api/v1/trial")
def trial():
    body = request.get_json(silent=True) or {}
    machine_id = str(body.get("machine_id", "")).strip()
    if len(machine_id) < 16 or len(machine_id) > 256 or body.get("product") != "NEXUS_ERP":
        return jsonify({"error":"invalid_request"}), 400
    now = utc_now()
    connection = connect()
    row = connection.execute("SELECT * FROM trial_devices WHERE machine_id=?", (machine_id,)).fetchone()
    if not row:
        first_seen = now
        trial_ends_at = now + timedelta(days=15)
        connection.execute("INSERT INTO trial_devices(machine_id,first_seen,trial_ends_at) VALUES(?,?,?)",
            (machine_id, iso(first_seen), iso(trial_ends_at)))
        connection.commit()
    else:
        first_seen, trial_ends_at = parse_time(row["first_seen"]), parse_time(row["trial_ends_at"])
    connection.close()
    token = make_trial_token(machine_id, first_seen, trial_ends_at)
    return jsonify({"first_seen":iso(first_seen),"trial_ends_at":iso(trial_ends_at),
                    "days_remaining":max(0, int((trial_ends_at-now).total_seconds()+86399)//86400),
                    "status":"trial" if trial_ends_at > now else "expired","token":token})

@app.post("/api/v1/activate")
def activate():
    body = request.get_json(silent=True) or {}
    code = str(body.get("activation_code", "")).strip()
    machine_id = str(body.get("machine_id", "")).strip()
    if not code or not machine_id or body.get("product") != "NEXUS_ERP":
        return jsonify({"error":"invalid_request"}), 400
    connection = connect()
    row = connection.execute("SELECT * FROM licenses WHERE code_hash=?", (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
    if not row or row["revoked"] or parse_time(row["expires_at"]) <= utc_now():
        connection.close()
        return jsonify({"error":"invalid_or_expired_code"}), 403
    if row["device_id"] and row["device_id"] != machine_id:
        connection.close()
        return jsonify({"error":"code_already_bound_to_another_device"}), 409
    if not row["device_id"]:
        connection.execute("UPDATE licenses SET device_id=? WHERE id=?", (machine_id, row["id"]))
        connection.commit()
        row = connection.execute("SELECT * FROM licenses WHERE id=?", (row["id"],)).fetchone()
    token = make_token(row, machine_id)
    connection.close()
    return jsonify({"token":token})

@app.post("/api/v1/validate")
def validate():
    body = request.get_json(silent=True) or {}
    machine_id = str(body.get("machine_id", "")).strip()
    payload = decode_token(body.get("token", ""), machine_id)
    if not payload or body.get("product") != "NEXUS_ERP":
        return jsonify({"error":"invalid_token"}), 403
    connection = connect()
    row = connection.execute("SELECT * FROM licenses WHERE id=?", (payload.get("license_id"),)).fetchone()
    if not row or row["revoked"] or row["device_id"] != machine_id or parse_time(row["expires_at"]) <= utc_now():
        connection.close()
        return jsonify({"error":"license_inactive"}), 403
    token = make_token(row, machine_id)
    connection.close()
    return jsonify({"token":token})

@app.post("/admin/licenses")
def create_license():
    if not admin_authorized():
        return jsonify({"error":"unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    customer = str(body.get("customer", "")).strip()
    license_type = str(body.get("license_type", "annual")).strip().lower()
    expires_text = str(body.get("expires_at", "")).strip()
    if not customer or len(customer) > 160 or license_type not in {"monthly","annual","lifetime","custom"}:
        return jsonify({"error":"invalid_customer_or_type"}), 400
    try:
        expiry = parse_time(expires_text)
    except (ValueError, TypeError, KeyError):
        return jsonify({"error":"invalid_expiry"}), 400
    if expiry <= utc_now():
        return jsonify({"error":"expiry_must_be_future"}), 400
    code = "NEX-" + secrets.token_urlsafe(24)
    license_id = uuid.uuid4().hex
    connection = connect()
    connection.execute("INSERT INTO licenses(id,code_hash,customer,license_type,expires_at,created_at) VALUES(?,?,?,?,?,?)",
        (license_id,hashlib.sha256(code.encode()).hexdigest(),customer,license_type,iso(expiry),iso(utc_now())))
    connection.commit()
    connection.close()
    return jsonify({"license_id":license_id,"activation_code":code,"customer":customer,"license_type":license_type,"expires_at":iso(expiry)}), 201

@app.post("/admin/licenses/<license_id>/revoke")
def revoke_license(license_id):
    if not admin_authorized():
        return jsonify({"error":"unauthorized"}), 401
    connection = connect()
    cursor = connection.execute("UPDATE licenses SET revoked=1 WHERE id=?", (license_id,))
    connection.commit()
    connection.close()
    if not cursor.rowcount:
        return jsonify({"error":"not_found"}), 404
    return jsonify({"status":"revoked","license_id":license_id})

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST","127.0.0.1"),port=int(os.environ.get("PORT","8080")),debug=False)
