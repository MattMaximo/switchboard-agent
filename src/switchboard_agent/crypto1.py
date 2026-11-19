"""Switchboard v1 message encryption: RFC 9180 HPKE + Ed25519 sender signatures.

Built on pyca/cryptography (`cryptography.hazmat.primitives.hpke`, added in 47.0).

Identity
    pub1  = X25519 public (32 B) || Ed25519 public (32 B)      -> 128 hex chars
    priv1 = X25519 private (32 B) || Ed25519 private seed (32 B)

Blob (base64 of)
    0x01 || enc (32 B) || HPKE ciphertext
    HPKE base mode, suite DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM
    info = "switchboard-v1" || sender pub1 || recipient pub1

HPKE plaintext
    u32 len || body (UTF-8 JSON) || Ed25519 signature (64 B) || zero padding to a 512-byte bucket
    signature = Ed25519(sender, "switchboard-v1/sig" || info || body)

The signature binds the sender's identity, the recipient's key and the exact
body; HPKE's key schedule binds the same info, so tampering, a forged sender,
or the wrong recipient all fail. Replay and sender/recipient *agent ids* are
checked one level up (client.py) from fields inside the signed body.
"""
import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite

VERSION = 1
VER_BYTE = b"\x01"
LABEL = b"switchboard-v1"
SIG_LABEL = b"switchboard-v1/sig"
REKEY_LABEL = b"switchboard-v1/rekey"
JOIN_LABEL = b"switchboard-v1/join"
SUITE = Suite(KEM.X25519, KDF.HKDF_SHA256, AEAD.AES_128_GCM)
SUITE_NAME = {"kem": "DHKEM(X25519, HKDF-SHA256)", "kdf": "HKDF-SHA256", "aead": "AES-128-GCM"}
PAD = 512
ENC_LEN = 32
_RAW = dict(encoding=ser.Encoding.Raw, format=ser.PublicFormat.Raw)


class CryptoError(ValueError):
    """A message failed to decrypt or verify. The text is safe to show."""


def new_identity():
    kx = x25519.X25519PrivateKey.generate()
    ks = ed25519.Ed25519PrivateKey.generate()
    priv = kx.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()) + \
        ks.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
    pub = kx.public_key().public_bytes(**_RAW) + ks.public_key().public_bytes(**_RAW)
    return priv.hex(), pub.hex()


def valid_pub(pub_hex):
    try:
        b = bytes.fromhex(pub_hex or "")
    except ValueError:
        return False
    if len(b) != 64:
        return False
    try:
        x25519.X25519PublicKey.from_public_bytes(b[:32])
        ed25519.Ed25519PublicKey.from_public_bytes(b[32:])
    except ValueError:
        return False
    return True


def _pub(pub_hex):
    if not valid_pub(pub_hex):
        raise CryptoError("invalid v1 public key")
    b = bytes.fromhex(pub_hex)
    return x25519.X25519PublicKey.from_public_bytes(b[:32]), ed25519.Ed25519PublicKey.from_public_bytes(b[32:])


def _priv(priv_hex):
    b = bytes.fromhex(priv_hex)
    if len(b) != 64:
        raise CryptoError("invalid v1 private key")
    return x25519.X25519PrivateKey.from_private_bytes(b[:32]), ed25519.Ed25519PrivateKey.from_private_bytes(b[32:])


def info(sender_pub, recipient_pub):
    return LABEL + bytes.fromhex(sender_pub) + bytes.fromhex(recipient_pub)


def seal(my_priv, my_pub, their_pub, obj):
    """Encrypt `obj` (JSON-serializable) from me to them. Returns base64 text."""
    _, sk = _priv(my_priv)
    rx, _ = _pub(their_pub)
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    inf = info(my_pub, their_pub)
    sig = sk.sign(SIG_LABEL + inf + body)
    pt = len(body).to_bytes(4, "big") + body + sig
    pt += b"\x00" * ((-len(pt)) % PAD)
    return base64.b64encode(VER_BYTE + SUITE.encrypt(pt, rx, info=inf)).decode()


def blob_version(blob):
    try:
        raw = base64.b64decode(blob[:8] + "=" * (-len(blob[:8]) % 4))
    except Exception:
        return None
    return raw[0] if raw else None


def unseal(my_priv, my_pub, their_pub, blob):
    """Decrypt and verify a v1 blob claimed to come from `their_pub`. Raises CryptoError."""
    try:
        raw = base64.b64decode(blob, validate=True)
    except Exception:
        raise CryptoError("message is not valid base64")
    if raw[:1] != VER_BYTE:
        raise CryptoError("not a v1 message")
    if len(raw) < 1 + ENC_LEN + 16 + PAD:
        raise CryptoError("message is too short")
    kx, _ = _priv(my_priv)
    _, spk = _pub(their_pub)
    inf = info(their_pub, my_pub)
    try:
        pt = SUITE.decrypt(raw[1:], kx, info=inf)
    except (InvalidTag, ValueError):
        raise CryptoError("message was tampered with, is not for this agent, or is not from this sender")
    n = int.from_bytes(pt[:4], "big")
    if 4 + n + 64 > len(pt):
        raise CryptoError("malformed message")
    body, sig = pt[4:4 + n], pt[4 + n:4 + n + 64]
    if any(pt[4 + n + 64:]):
        raise CryptoError("malformed padding")
    try:
        spk.verify(sig, SIG_LABEL + inf + body)
    except InvalidSignature:
        raise CryptoError("sender signature is invalid (forged sender)")
    try:
        return json.loads(body)
    except ValueError:
        raise CryptoError("malformed message body")


def sign(my_priv, data):
    _, sk = _priv(my_priv)
    return sk.sign(data).hex()


# Outer signature (0.8): lets the relay refuse a changed, misdirected or forged blob BEFORE it stores it or
# wakes anyone, without seeing the contents (the inner signature is only checkable after decryption).
#   osig = hex(Ed25519(sender, OUTER_LABEL || ctx || u16be(len(to)) || to || SHA-256(blob as sent)))
#   ctx 1 = agent -> agent (send, each group item, file message); to = recipient agent id
#   ctx 2 = outside A2A caller -> agent;                           to = recipient pub1 (hex, from its Agent Card)
#   ctx 3 = agent's A2A reply;                                     to = task id
OUTER_LABEL = b"switchboard-v1/outer"
CTX_AGENT, CTX_A2A_IN, CTX_A2A_REPLY = 1, 2, 3


def outer_msg(ctx, to, blob):
    t = str(to).encode()
    return OUTER_LABEL + bytes([ctx]) + len(t).to_bytes(2, "big") + t + hashlib.sha256(str(blob).encode()).digest()


def outer_sig(my_priv, ctx, to, blob):
    return sign(my_priv, outer_msg(ctx, to, blob))


def rekey_proof(my_priv, agent_id, new_pub):
    """Proof that we hold the new key, bound to our agent id (prevents key confusion)."""
    return sign(my_priv, REKEY_LABEL + agent_id.encode() + bytes.fromhex(new_pub))


def join_proof(my_priv, new_pub):
    return sign(my_priv, JOIN_LABEL + bytes.fromhex(new_pub))


def fingerprint(pub_hex):
    return hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()[:12]
