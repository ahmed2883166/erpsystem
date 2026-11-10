import base64, json, tempfile, unittest
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from licensing import get_license_status, verify_license_token

class LicensingTests(unittest.TestCase):
    def test_trial_expires_after_15_days(self):
        with tempfile.TemporaryDirectory() as folder:
            start=datetime(2026,11,10,tzinfo=timezone.utc)
            self.assertEqual(get_license_status(folder,start,machine_id="device-a")["days_remaining"],15)
            self.assertEqual(get_license_status(folder,start+timedelta(days=14),machine_id="device-a")["status"],"trial")
            self.assertEqual(get_license_status(folder,start+timedelta(days=15),machine_id="device-a")["status"],"expired")

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
