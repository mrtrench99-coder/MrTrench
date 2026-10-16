"""Generate a VAPID key pair for browser push notifications."""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


private_key = ec.generate_private_key(ec.SECP256R1())
private_der = private_key.private_bytes(
    encoding=serialization.Encoding.DER,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
public_numbers = private_key.public_key().public_numbers()
public_uncompressed = (
    b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
)

print("VAPID_PUBLIC_KEY=" + b64url(public_uncompressed))
print("VAPID_PRIVATE_KEY=" + b64url(private_der))
print("VAPID_SUBJECT=mailto:mrtrench99@gmail.com")
