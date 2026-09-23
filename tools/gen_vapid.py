"""Generate a VAPID keypair for web push — run once, then paste the three
values into Render env (VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT).

    python tools/gen_vapid.py

The PRIVATE key is a secret: put it in Render env, NEVER commit it (the repo
is public). The PUBLIC key is what the panel's JS subscribes with; it is not
secret. VAPID_SUBJECT is any contact URL the push services can see — the
panel URL or a mailto: both work.
"""
import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main():
    v = Vapid02()
    v.generate_keys()
    priv_raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    pub_raw = v.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    print("VAPID_PUBLIC_KEY =", _b64u(pub_raw))
    print("VAPID_PRIVATE_KEY =", _b64u(priv_raw))
    print("VAPID_SUBJECT = mailto:you@example.com   # or your panel URL")


if __name__ == "__main__":
    main()
