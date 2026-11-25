"""Generate an Ed25519 key pair for the NEXUS license server."""
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

private = Ed25519PrivateKey.generate()
private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
print("NEXUS_LICENSE_PRIVATE_KEY=" + base64.b64encode(private_raw).decode())
print("NEXUS_LICENSE_PUBLIC_KEY=" + base64.b64encode(public_raw).decode())
print("\\nKeep PRIVATE_KEY only on the licensing server. Never put it in the ERP client or Git.")
