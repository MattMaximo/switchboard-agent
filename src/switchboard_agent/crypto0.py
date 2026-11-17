"""End-to-end encryption for Switchboard v0 messages (standard library only).

Scheme: static + ephemeral finite-field Diffie-Hellman over the RFC 3526
2048-bit MODP group, HKDF-SHA256 key derivation, HMAC-SHA256 counter-mode
keystream, and an HMAC-SHA256 tag over the whole ciphertext (encrypt-then-MAC).
Plaintext is padded to 512-byte buckets so sizes reveal little.

Mixing the sender's static key into the shared secret authenticates the sender:
a blob only opens with the real sender's public key.

This is a v0 scheme built from standard primitives so the package has zero
dependencies. It has not been independently audited; v1 (crypto1.py) uses
RFC 9180 HPKE + Ed25519 from pyca/cryptography. v0 is kept only to read old mail and to talk to peers that have not upgraded yet.
"""
import base64
import hashlib
import hmac
import json
import secrets

P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
G = 2
NB = 256


def i2b(n):
    return n.to_bytes(NB, "big")


def hkdf(ikm, salt, info, n):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, t, c = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([c]), hashlib.sha256).digest()
        out += t
        c += 1
    return out[:n]


def keystream(key, n):
    out, c = b"", 0
    while len(out) < n:
        out += hmac.new(key, c.to_bytes(8, "big"), hashlib.sha256).digest()
        c += 1
    return out[:n]


def new_keypair():
    priv = secrets.randbits(320)
    return priv, pow(G, priv, P)


def _valid(x):
    return 1 < x < P - 1


def seal(my_priv, my_pub, their_pub, obj):
    if not _valid(their_pub):
        raise ValueError("bad recipient key")
    pt = json.dumps(obj).encode()
    pt = len(pt).to_bytes(4, "big") + pt
    pt += secrets.token_bytes((-len(pt)) % 512)
    e = secrets.randbits(320)
    E = pow(G, e, P)
    ikm = i2b(pow(their_pub, e, P)) + i2b(pow(their_pub, my_priv, P))
    k = hkdf(ikm, b"switchboard-v0", b"sb0" + i2b(E) + i2b(my_pub) + i2b(their_pub), 64)
    ct = bytes(a ^ b for a, b in zip(pt, keystream(k[:32], len(pt))))
    tag = hmac.new(k[32:], i2b(E) + ct, hashlib.sha256).digest()
    return base64.b64encode(b"\x00" + i2b(E) + ct + tag).decode()


def unseal(my_priv, my_pub, their_pub, blob):
    raw = base64.b64decode(blob)
    if raw[:1] != b"\x00" or len(raw) < 1 + NB + 32:
        raise ValueError("unsupported message version")
    E = int.from_bytes(raw[1:1 + NB], "big")
    if not _valid(E) or not _valid(their_pub):
        raise ValueError("bad key")
    ct, tag = raw[1 + NB:-32], raw[-32:]
    ikm = i2b(pow(E, my_priv, P)) + i2b(pow(their_pub, my_priv, P))
    k = hkdf(ikm, b"switchboard-v0", b"sb0" + i2b(E) + i2b(their_pub) + i2b(my_pub), 64)
    if not hmac.compare_digest(tag, hmac.new(k[32:], i2b(E) + ct, hashlib.sha256).digest()):
        raise ValueError("message was tampered with or is not from this sender")
    pt = bytes(a ^ b for a, b in zip(ct, keystream(k[:32], len(ct))))
    n = int.from_bytes(pt[:4], "big")
    return json.loads(pt[4:4 + n])


def fingerprint(pub_hex):
    pub_hex = pub_hex if len(pub_hex) % 2 == 0 else "0" + pub_hex
    return hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()[:12]
