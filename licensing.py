"""Hybrid trial and signed-license enforcement for NEXUS ERP."""
import base64, hashlib, json, os, platform, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

TRIAL_DAYS = 15
NETWORK_TIMEOUT_SECONDS = 4
CLOCK_SKEW_SECONDS = 300

def utc_now():
    return datetime.now(timezone.utc)

def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

def _parse_time(value):
    if not value: return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def machine_fingerprint():
    """Hash a stable machine identifier; raw hardware identifiers are never sent."""
    source = ""
    if platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                source = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except (ImportError, OSError): pass
    if not source and platform.system() == "Linux":
        try: source = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        except OSError: pass
    if not source: source = f"{platform.node()}:{uuid.getnode()}"
    return hashlib.sha256(f"NEXUS-ERP|{platform.system()}|{source}".encode()).hexdigest()

def _registry_read():
    if platform.system() != "Windows": return {}
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\NEXUSERP", 0, winreg.KEY_READ) as key:
            values = {}
            for name in ("first_seen", "last_seen"):
                try: values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError: pass
            return values
    except (ImportError, OSError): return {}

def _registry_write(first_seen, last_seen):
    if platform.system() != "Windows": return
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\NEXUSERP", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "first_seen", 0, winreg.REG_SZ, first_seen)
            winreg.SetValueEx(key, "last_seen", 0, winreg.REG_SZ, last_seen)
    except (ImportError, OSError): pass

def _state_path(data_dir=None):
    root = Path(data_dir) if data_dir else Path(__file__).resolve().parent / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "nexus_license.json"

def _read_state(data_dir=None):
    try:
        value = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError): return {}

def _write_state(state, data_dir=None):
    path = _state_path(data_dir)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)

def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

def verify_license_token(token, expected_machine=None, public_key=None, current_time=None):
    """Verify base64url(payload JSON).base64url(Ed25519 signature)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        parts = (token or "").split(".")
        if len(parts) != 2: return None
        payload_bytes = _b64decode(parts[0])
        key_text = public_key if public_key is not None else os.environ.get("NEXUS_LICENSE_PUBLIC_KEY", "")
        if not key_text: return None
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key_text)).verify(_b64decode(parts[1]), parts[0].encode("ascii"))
        payload = json.loads(payload_bytes.decode("utf-8"))
        if expected_machine and payload.get("machine_id") != expected_machine: return None
        now = current_time or utc_now()
        expiry, offline_until = _parse_time(payload.get("expires_at")), _parse_time(payload.get("offline_until"))
        if not expiry or not offline_until or expiry <= now or offline_until > expiry: return None
        return payload
    except Exception: return None

def _post_json(url, payload):
    if not url: raise RuntimeError("خادم التراخيص غير مضبوط بعد.")
    request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json","Accept":"application/json"}, method="POST")
    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict): raise RuntimeError("استجابة خادم التراخيص غير صالحة.")
            return result
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("تعذر الاتصال بخادم التراخيص. تحقق من الإنترنت وحاول مرة أخرى.") from exc

def activate_license(activation_code, data_dir=None, machine_id=None, current_time=None):
    code = (activation_code or "").strip()
    if len(code) < 8 or len(code) > 160: raise RuntimeError("أدخل كود تفعيل صحيحًا.")
    machine_id = machine_id or machine_fingerprint()
    result = _post_json(os.environ.get("NEXUS_LICENSE_ACTIVATE_URL", ""), {"activation_code":code,"machine_id":machine_id,"product":"NEXUS_ERP"})
    token = result.get("token", "")
    payload = verify_license_token(token, machine_id, current_time=current_time)
    if not payload: raise RuntimeError("رفض البرنامج رخصة التفعيل؛ التوقيع أو الجهاز أو تاريخ الصلاحية غير صحيح.")
    state = _read_state(data_dir)
    state.update({"machine_id":machine_id,"token":token,"activated_at":_iso(current_time or utc_now()),"last_seen":_iso(current_time or utc_now()),"license_type":payload.get("license_type","licensed"),"customer":payload.get("customer","")})
    _write_state(state, data_dir)
    return payload

def _refresh_license(state, machine_id, data_dir, current_time):
    try:
        result = _post_json(os.environ.get("NEXUS_LICENSE_VALIDATE_URL",""), {"token":state["token"],"machine_id":machine_id,"product":"NEXUS_ERP"})
        token = result.get("token","")
        payload = verify_license_token(token,machine_id,current_time=current_time)
        if not payload: return False
        state.update({"token":token,"last_online_check":_iso(current_time),"license_type":payload.get("license_type","licensed"),"customer":payload.get("customer","")})
        _write_state(state,data_dir)
        return True
    except (RuntimeError, KeyError): return False

def get_license_status(data_dir=None, current_time=None, machine_id=None, public_key=None):
    current_time = current_time or utc_now()
    machine_id = machine_id or machine_fingerprint()
    state, registry = _read_state(data_dir), _registry_read()
    if state.get("machine_id") and state["machine_id"] != machine_id:
        return {"status":"device_changed","message":"تم نقل قاعدة الترخيص إلى جهاز آخر.","machine_id":machine_id}
    state["machine_id"] = machine_id
    firsts = [_parse_time(state.get("first_seen")),_parse_time(registry.get("first_seen"))]
    first_seen = min((x for x in firsts if x),default=None) or current_time
    lasts = [_parse_time(state.get("last_seen")),_parse_time(registry.get("last_seen"))]
    last_seen = max((x for x in lasts if x),default=None)
    if last_seen and current_time < last_seen - timedelta(seconds=CLOCK_SKEW_SECONDS):
        return {"status":"clock_rollback","message":"تم اكتشاف تغيير أو إرجاع ساعة الجهاز.","machine_id":machine_id}
    state["first_seen"],state["last_seen"] = _iso(first_seen),_iso(max(current_time,last_seen) if last_seen else current_time)
    _write_state(state,data_dir)
    _registry_write(state["first_seen"],state["last_seen"])
    if state.get("token"):
        payload = verify_license_token(state["token"],machine_id,public_key,current_time)
        if payload:
            if current_time > _parse_time(payload["offline_until"]):
                if _refresh_license(state,machine_id,data_dir,current_time):
                    payload = verify_license_token(state["token"],machine_id,public_key,current_time)
                    if payload: return {"status":"licensed","customer":payload.get("customer",""),"license_type":payload.get("license_type","licensed"),"expires_at":payload.get("expires_at"),"machine_id":machine_id}
                return {"status":"online_required","message":"انتهت فترة العمل دون اتصال؛ اتصل بالإنترنت لتجديد التحقق.","machine_id":machine_id}
            return {"status":"licensed","customer":payload.get("customer",""),"license_type":payload.get("license_type","licensed"),"expires_at":payload.get("expires_at"),"machine_id":machine_id}
        return {"status":"license_invalid","message":"الرخصة غير صالحة أو منتهية.","machine_id":machine_id}
    remaining = (first_seen + timedelta(days=TRIAL_DAYS) - current_time).total_seconds()
    if remaining <= 0: return {"status":"expired","days_remaining":0,"machine_id":machine_id}
    return {"status":"trial","days_remaining":max(1,int((remaining+86399)//86400)),"trial_ends_at":_iso(first_seen+timedelta(days=TRIAL_DAYS)),"machine_id":machine_id}
