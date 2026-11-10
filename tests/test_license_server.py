import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

import license_server

class LicenseServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old={k:os.environ.get(k) for k in ("NEXUS_LICENSE_DB","NEXUS_LICENSE_PRIVATE_KEY","NEXUS_LICENSE_ADMIN_TOKEN")}
        key=Ed25519PrivateKey.generate()
        raw=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption())
        os.environ["NEXUS_LICENSE_DB"]=self.temp.name+"/licenses.db"
        os.environ["NEXUS_LICENSE_PRIVATE_KEY"]=base64.b64encode(raw).decode()
        os.environ["NEXUS_LICENSE_ADMIN_TOKEN"]="test-admin"
        self.client=license_server.app.test_client()

    def tearDown(self):
        for k,v in self.old.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v
        self.temp.cleanup()

    def test_create_activate_bind_and_validate(self):
        expiry=(datetime.now(timezone.utc)+timedelta(days=90)).isoformat()
        created=self.client.post("/admin/licenses",json={"customer":"شركة اختبار","license_type":"annual","expires_at":expiry},headers={"Authorization":"Bearer test-admin"})
        self.assertEqual(created.status_code,201)
        code=created.json["activation_code"]
        activated=self.client.post("/api/v1/activate",json={"activation_code":code,"machine_id":"device-1","product":"NEXUS_ERP"})
        self.assertEqual(activated.status_code,200)
        token=activated.json["token"]
        checked=self.client.post("/api/v1/validate",json={"token":token,"machine_id":"device-1","product":"NEXUS_ERP"})
        self.assertEqual(checked.status_code,200)
        denied=self.client.post("/api/v1/activate",json={"activation_code":code,"machine_id":"device-2","product":"NEXUS_ERP"})
        self.assertEqual(denied.status_code,409)

    def test_trial_is_persisted_per_device(self):
        first=self.client.post("/api/v1/trial",json={"machine_id":"device-machine-0001","product":"NEXUS_ERP"})
        again=self.client.post("/api/v1/trial",json={"machine_id":"device-machine-0001","product":"NEXUS_ERP"})
        other=self.client.post("/api/v1/trial",json={"machine_id":"device-machine-0002","product":"NEXUS_ERP"})
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.json["first_seen"],again.json["first_seen"])
        self.assertNotEqual(first.json["first_seen"],other.json["first_seen"])

    def test_admin_requires_secret(self):
        response=self.client.post("/admin/licenses",json={})
        self.assertEqual(response.status_code,401)

if __name__=="__main__": unittest.main()
