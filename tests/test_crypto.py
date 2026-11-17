import base64
import unittest

from switchboard_agent import crypto0, crypto1
from switchboard_agent.crypto1 import CryptoError


class V1(unittest.TestCase):
    def setUp(self):
        self.a = crypto1.new_identity()
        self.b = crypto1.new_identity()
        self.m = crypto1.new_identity()
        self.msg = {"type": "text", "text": "hello ✓ " + "x" * 40, "mid": "abc"}

    def seal(self, src, dst, obj=None):
        return crypto1.seal(src[0], src[1], dst[1], obj or self.msg)

    def test_roundtrip(self):
        blob = self.seal(self.a, self.b)
        self.assertEqual(crypto1.blob_version(blob), 1)
        self.assertEqual(crypto1.unseal(self.b[0], self.b[1], self.a[1], blob), self.msg)

    def test_pub_format(self):
        self.assertEqual(len(self.a[1]), 128)
        self.assertTrue(crypto1.valid_pub(self.a[1]))
        self.assertFalse(crypto1.valid_pub("00" * 63))
        self.assertFalse(crypto1.valid_pub("zz" * 64))

    def test_padding_buckets(self):
        raw = base64.b64decode(self.seal(self.a, self.b))
        self.assertEqual((len(raw) - 1 - 32 - 16) % 512, 0)
        small = len(base64.b64decode(self.seal(self.a, self.b, {"t": "a"})))
        big = len(base64.b64decode(self.seal(self.a, self.b, {"t": "a" * 300})))
        self.assertEqual(small, big)

    def test_tamper_rejected(self):
        raw = bytearray(base64.b64decode(self.seal(self.a, self.b)))
        for pos in (1, 40, len(raw) - 1):
            r = bytearray(raw)
            r[pos] ^= 1
            with self.assertRaises(CryptoError):
                crypto1.unseal(self.b[0], self.b[1], self.a[1], base64.b64encode(bytes(r)).decode())

    def test_forged_sender_rejected(self):
        # Mallory knows A's public key and B's, but signs with her own key.
        forged = crypto1.seal(self.m[0], self.a[1], self.b[1], self.msg)
        with self.assertRaisesRegex(CryptoError, "forged"):
            crypto1.unseal(self.b[0], self.b[1], self.a[1], forged)
        # Honest Mallory blob presented as coming from A fails at HPKE (info binds sender key).
        with self.assertRaises(CryptoError):
            crypto1.unseal(self.b[0], self.b[1], self.a[1], self.seal(self.m, self.b))

    def test_wrong_recipient_rejected(self):
        blob = self.seal(self.a, self.b)
        with self.assertRaises(CryptoError):
            crypto1.unseal(self.m[0], self.m[1], self.a[1], blob)

    def test_v0_blob_not_v1(self):
        p0, P0 = crypto0.new_keypair()
        q0, Q0 = crypto0.new_keypair()
        blob = crypto0.seal(p0, P0, Q0, self.msg)
        self.assertEqual(crypto1.blob_version(blob), 0)
        with self.assertRaises(CryptoError):
            crypto1.unseal(self.b[0], self.b[1], self.a[1], blob)
        self.assertEqual(crypto0.unseal(q0, Q0, P0, blob), self.msg)

    def test_proofs_verify(self):
        from cryptography.hazmat.primitives.asymmetric import ed25519
        pk = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.a[1])[32:])
        sig = bytes.fromhex(crypto1.rekey_proof(self.a[0], "aid1", self.a[1]))
        pk.verify(sig, crypto1.REKEY_LABEL + b"aid1" + bytes.fromhex(self.a[1]))
        sig = bytes.fromhex(crypto1.join_proof(self.a[0], self.a[1]))
        pk.verify(sig, crypto1.JOIN_LABEL + bytes.fromhex(self.a[1]))

    def test_rfc9180_interop(self):
        """Open our blob with an independent RFC 9180 implementation, if available."""
        try:
            from pyhpke import AEADId, CipherSuite, KDFId, KEMId
        except ImportError:
            self.skipTest("pyhpke not installed")
        ps = CipherSuite.new(KEMId.DHKEM_X25519_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES128_GCM)
        raw = base64.b64decode(self.seal(self.a, self.b))[1:]
        sk = ps.kem.deserialize_private_key(bytes.fromhex(self.b[0])[:32])
        pt = ps.create_recipient_context(raw[:32], sk, info=crypto1.info(self.a[1], self.b[1])).open(raw[32:])
        n = int.from_bytes(pt[:4], "big")
        import json
        self.assertEqual(json.loads(pt[4:4 + n]), self.msg)


if __name__ == "__main__":
    unittest.main()
