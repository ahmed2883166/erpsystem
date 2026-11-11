import base64, json, tempfile, unittest, os
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import licensing
from licensing import get_license_status, verify_license_token

class LicensingTests(unittest.TestCase):
    def test_missing_remote_trial_authority_fails_closed(self):
        import os
        old = os.environ.pop("NEXUS_LICENSE_TRIAL_URL", None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                start=datetime(2026,11,10,tzinfo=timezone.utc)
                status = get_license_status(folder,start,machine_id="device-a")
                self.assertEqual(status["status"],"online_required")
        finally:
            if old is not None:
                os.environ["NEXUS_LICENSE_TRIAL_URL"] = old

    def test_trial_requires_a_valid_server_signature(self):
        private=Ed25519PrivateKey.generate()
        public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        public_text=base64.b64encode(public).decode()
        now=datetime(2026,11,10,tzinfo=timezone.utc)
        expiry=now+timedelta(days=15)
        payload={"machine_id":"device-a","license_type":"trial","expires_at":expiry.isoformat(),
                 "offline_until":expiry.isoformat(),"trial_ends_at":expiry.isoformat()}
        encoded=base64.urlsafe_b64encode(json.dumps(payload,separators=(",",":")).encode()).decode().rstrip("=")
        token=encoded+"."+base64.urlsafe_b64encode(private.sign(encoded.encode("ascii"))).decode().rstrip("=")
        old_url=os.environ.get("NEXUS_LICENSE_TRIAL_URL")
        old_key=os.environ.get("NEXUS_LICENSE_PUBLIC_KEY")
        os.environ["NEXUS_LICENSE_TRIAL_URL"]="https://licenses.example.test/api/v1/trial"
        os.environ["NEXUS_LICENSE_PUBLIC_KEY"]=public_text
        try:
            with tempfile.TemporaryDirectory() as folder, patch.object(
                licensing, "_post_json", return_value={"first_seen":now.isoformat(),"trial_ends_at":expiry.isoformat(),"status":"trial","token":token}
            ):
                status=get_license_status(folder,now,machine_id="device-a")
                self.assertEqual(status["status"],"trial")
                self.assertEqual(status["trial_authority"],"signed_server")
            with tempfile.TemporaryDirectory() as folder, patch.object(
                licensing, "_post_json", return_value={"first_seen":now.isoformat(),"trial_ends_at":expiry.isoformat(),"status":"trial","token":token+"x"}
            ):
                status=get_license_status(folder,now,machine_id="device-a")
                self.assertEqual(status["status"],"online_required")
        finally:
            if old_url is None: os.environ.pop("NEXUS_LICENSE_TRIAL_URL",None)
            else: os.environ["NEXUS_LICENSE_TRIAL_URL"]=old_url
            if old_key is None: os.environ.pop("NEXUS_LICENSE_PUBLIC_KEY",None)
            else: os.environ["NEXUS_LICENSE_PUBLIC_KEY"]=old_key

    def test_cached_trial_requires_server_and_server_expiry_wins_over_rolled_back_clock(self):
        private=Ed25519PrivateKey.generate()
        public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        public_text=base64.b64encode(public).decode()
        now=datetime(2026,11,10,tzinfo=timezone.utc)
        expiry=now+timedelta(days=15)
        payload={"machine_id":"device-a","license_type":"trial","expires_at":expiry.isoformat(),
                 "offline_until":expiry.isoformat(),"trial_ends_at":expiry.isoformat()}
        encoded=base64.urlsafe_b64encode(json.dumps(payload,separators=(",",":")).encode()).decode().rstrip("=")
        token=encoded+"."+base64.urlsafe_b64encode(private.sign(encoded.encode("ascii"))).decode().rstrip("=")
        old_url=os.environ.get("NEXUS_LICENSE_TRIAL_URL")
        old_key=os.environ.get("NEXUS_LICENSE_PUBLIC_KEY")
        os.environ["NEXUS_LICENSE_TRIAL_URL"]="https://licenses.example.test/api/v1/trial"
        os.environ["NEXUS_LICENSE_PUBLIC_KEY"]=public_text
        try:
            with tempfile.TemporaryDirectory() as folder:
                with patch.object(licensing,"_post_json",return_value={
                    "first_seen":now.isoformat(),"trial_ends_at":expiry.isoformat(),"status":"trial","token":token
                }):
                    self.assertEqual(get_license_status(folder,now,machine_id="device-a")["status"],"trial")
                with patch.object(licensing,"_post_json",side_effect=RuntimeError("offline")):
                    self.assertEqual(get_license_status(folder,now+timedelta(days=1),machine_id="device-a")["status"],"online_required")
                with patch.object(licensing,"_post_json",return_value={
                    "first_seen":now.isoformat(),"trial_ends_at":expiry.isoformat(),"status":"expired","token":token
                }):
                    self.assertEqual(get_license_status(folder,now+timedelta(days=1),machine_id="device-a")["status"],"expired")
        finally:
            if old_url is None: os.environ.pop("NEXUS_LICENSE_TRIAL_URL",None)
            else: os.environ["NEXUS_LICENSE_TRIAL_URL"]=old_url
            if old_key is None: os.environ.pop("NEXUS_LICENSE_PUBLIC_KEY",None)
            else: os.environ["NEXUS_LICENSE_PUBLIC_KEY"]=old_key

    def test_clock_rollback_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            start=datetime(2026,11,10,tzinfo=timezone.utc)
            get_license_status(folder,start,machine_id="device-a")
            get_license_status(folder,start+timedelta(days=2),machine_id="device-a")
            self.assertEqual(get_license_status(folder,start+timedelta(days=1),machine_id="device-a")["status"],"clock_rollback")

    def test_signed_license_is_bound_to_machine(self):
        private=Ed25519PrivateKey.generate()
        public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        now=datetime(2026,11,10,tzinfo=timezone.utc)
        payload={"machine_id":"device-a","expires_at":(now+timedelta(days=365)).isoformat(),"offline_until":(now+timedelta(days=7)).isoformat(),"license_type":"annual","customer":"Test"}
        encoded=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token=encoded+"."+base64.urlsafe_b64encode(private.sign(encoded.encode("ascii"))).decode().rstrip("=")
        key=base64.b64encode(public).decode()
        self.assertEqual(verify_license_token(token,"device-a",key,now)["customer"],"Test")
        self.assertIsNone(verify_license_token(token,"device-b",key,now))

if __name__=="__main__": unittest.main()
