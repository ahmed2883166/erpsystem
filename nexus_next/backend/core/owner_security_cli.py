"""Local-only owner licensing and encrypted backup utility."""
from __future__ import annotations
import argparse, getpass, json, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
import shutil
from core.backup_crypto import decrypt_backup, encrypt_backup
from core.offline_license import LicenseError, create_signed_license, generate_owner_keypair, windows_machine_fingerprint
def password(confirm=False):
    value=getpass.getpass("عبارة المرور (12 حرفًا على الأقل): ")
    if confirm and value != getpass.getpass("أعد إدخال عبارة المرور: "): raise ValueError("عبارتا المرور غير متطابقتين.")
    return value
def main():
    p=argparse.ArgumentParser(description="NEXUS Offline Owner Security"); sub=p.add_subparsers(dest="command",required=True)
    a=sub.add_parser("owner-init"); a.add_argument("--usb-dir",required=True)
    a=sub.add_parser("request"); a.add_argument("--installation-id",required=True); a.add_argument("--output",required=True)
    a=sub.add_parser("issue"); a.add_argument("--usb-dir",required=True); a.add_argument("--request",required=True); a.add_argument("--output",required=True)
    a=sub.add_parser("install-license"); a.add_argument("--usb-dir",required=True); a.add_argument("--license",required=True); a.add_argument("--installation-id",required=True); a.add_argument("--target-dir",required=True)
    a=sub.add_parser("encrypt-backup"); a.add_argument("--input",required=True); a.add_argument("--output",required=True)
    a=sub.add_parser("decrypt-backup"); a.add_argument("--input",required=True); a.add_argument("--output",required=True)
    args=p.parse_args()
    try:
        if args.command=="owner-init":
            usb=Path(args.usb_dir)
            if not usb.is_dir(): raise ValueError("مجلد USB غير موجود.")
            generate_owner_keypair(usb/"owner-private.pem",usb/"owner-public.pem",password(True))
            print("تم إنشاء مفتاح المالك على USB. احتفظ بنسخة استرداد آمنة منفصلة.")
        elif args.command=="request":
            data={"schema_version":1,"installation_id":args.installation_id.strip(),"machine_fingerprint":windows_machine_fingerprint(),"requested_at":datetime.now(timezone.utc).isoformat(),"request_id":str(uuid.uuid4())}
            if not data["installation_id"]: raise ValueError("معرّف التثبيت مطلوب.")
            out=Path(args.output)
            if out.exists(): raise ValueError("ملف الطلب موجود بالفعل؛ لن يتم استبداله.")
            out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8"); print(f"تم إنشاء الطلب: {out}")
        elif args.command=="issue":
            usb=Path(args.usb_dir); req=json.loads(Path(args.request).read_text(encoding="utf-8"))
            if req.get("schema_version")!=1: raise ValueError("إصدار الطلب غير مدعوم.")
            token=create_signed_license(usb/"owner-private.pem",password(),req["installation_id"],req["machine_fingerprint"],str(uuid.uuid4()),datetime.now(timezone.utc).isoformat())
            out=Path(args.output)
            if out.exists(): raise ValueError("ملف الترخيص موجود بالفعل؛ لن يتم استبداله.")
            out.parent.mkdir(parents=True,exist_ok=True); out.write_text(token,encoding="ascii"); print(f"تم إصدار الترخيص: {out}")
        elif args.command=="install-license":
            from core.offline_license import verify_signed_license
            usb=Path(args.usb_dir); target=Path(args.target_dir); target.mkdir(parents=True,exist_ok=True)
            public=usb/"owner-public.pem"; license_file=Path(args.license)
            verify_signed_license(license_file.read_text(encoding="ascii"),public,args.installation_id,windows_machine_fingerprint())
            destinations=[target/"owner-public.pem",target/"nexus.license"]
            if any(item.exists() for item in destinations): raise ValueError("ملفات الترخيص موجودة بالفعل؛ لن يتم استبدالها.")
            shutil.copy2(public,destinations[0])
            try: shutil.copy2(license_file,destinations[1])
            except Exception:
                destinations[0].unlink(missing_ok=True)
                raise
            print(f"تم تثبيت ملف الترخيص ومفتاح التحقق في: {target}")
        elif args.command in ("encrypt-backup","decrypt-backup"):
            pw=password(); result=encrypt_backup(args.input,args.output,pw) if args.command=="encrypt-backup" else decrypt_backup(args.input,args.output,pw); print(f"اكتملت العملية: {result}")
        return 0
    except (OSError,ValueError,KeyError,json.JSONDecodeError,LicenseError) as exc:
        print(f"خطأ: {exc}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
