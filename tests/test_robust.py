"""0.6.0 hardening: one bad message never breaks a mailbox, and no question is lost silently."""
import base64
import json
import os
import time
import unittest
from unittest import mock

from switchboard_agent import client, crypto1, listen, profiles

from test_ask import Base, stub


def raw_seal(my_priv, my_pub, their_pub, body_bytes):
    """crypto1.seal with an arbitrary (possibly non-object) JSON body, as a hand-written sender could."""
    _, sk = crypto1._priv(my_priv)
    rx, _ = crypto1._pub(their_pub)
    inf = crypto1.info(my_pub, their_pub)
    sig = sk.sign(crypto1.SIG_LABEL + inf + body_bytes)
    pt = len(body_bytes).to_bytes(4, "big") + body_bytes + sig
    pt += b"\x00" * ((-len(pt)) % crypto1.PAD)
    return base64.b64encode(crypto1.VER_BYTE + crypto1.SUITE.encrypt(pt, rx, info=inf)).decode()


class LeaseLoop:
    """Loopback relay with lease semantics: messages come back on every poll until acked."""

    def __init__(self, base):
        self.base, self.n, self.acked, self.fail_send = base, 0, set(), False
        self.box = {}

    def put(self, to, m):
        self.n += 1
        self.box.setdefault(to, []).append(dict(m, id=self.n))

    def pending(self, to, junk=False):
        return [m for m in self.box.get(to, []) if (junk if not isinstance(m, dict) else m["id"] not in self.acked)]

    def __call__(self, host, method, path, body=None, token=None, timeout=40):
        me = token.split(".")[1]
        if path.startswith("/api/agent/poll"):
            return {"self": {"answers_own": self.base.answers_own.get(me, True)}, "messages": self.pending(me, junk=True)}
        if path == "/api/agent/ack":
            self.acked.update(body["ids"])
            return {"ok": True}
        if path == "/api/agent/send":
            if self.fail_send:
                raise client.SwitchboardError("Can't reach the relay", "unreachable")
            self.put(body["to"], {"from": me, "blob": body["blob"], "sent": 1})
            return {"status": "delivered"}
        return self.base(host, method, path, body, token, timeout)


class Robust(Base):
    def setUp(self):
        super().setUp()
        self.lease = LeaseLoop(self.relay)
        self.patch.stop()
        self.patch = mock.patch.object(client, "api", self.lease)
        self.patch.start()

    def own(self, obj, frm=None):
        frm = frm or self.a
        body = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.lease.put("ab", {"from": frm["id"], "sent": 1, "blob": raw_seal(frm["priv1"], frm["pub1"], self.b["pub1"], body)})

    def test_bad_bodies_never_break_the_mailbox(self):
        stranger_priv, stranger_pub = crypto1.new_identity()
        ok = {"type": "text", "text": "real", "mid": "m1", "ts": 1, "from": "aa", "to": "ab"}
        bad = [
            lambda: self.lease.put("ab", {"from": "a2a", "sent": 1, "a2a": {"task": "t1", "caller_pub1": stranger_pub},
                                          "blob": raw_seal(stranger_priv, stranger_pub, self.b["pub1"], b"[]")}),
            lambda: self.lease.put("ab", {"from": "a2a", "sent": 1, "a2a": "not an object"}),
            lambda: self.own([]),
            lambda: self.own(dict(ok, mid=["x"])),
            lambda: self.own(dict(ok, mid=12345)),
            lambda: self.own(dict(ok, mid="m2", group={"id": "g1", "members": 7})),
            lambda: self.own(dict(ok, mid="m3", type="ask", qid=123456789012)),
            lambda: self.lease.box.setdefault("ab", []).append("not a dict"),
        ]
        for make in bad:
            make()
        self.own(ok)
        got = client.Agent("B").inbox()
        self.assertIn("real", [i.get("text") for i in got])
        self.assertEqual(self.lease.pending("ab"), [])
        for _ in range(2):
            self.assertEqual(client.Agent("B").inbox(), [])
        q = [i for i in got if i.get("text") == "x" and i["type"] != "unreadable"]
        self.assertTrue(all(i["type"] == "text" for i in q))  # the int qid ask is plain text

    def test_keep_failure_redelivers_instead_of_dropping(self):
        self.own({"type": "text", "text": "keep me", "mid": "k1", "ts": 1, "from": "aa", "to": "ab"})
        ag = client.Agent("B")

        def boom(items):
            raise OSError("disk full")
        with self.assertRaises(OSError):
            ag._fetch(0, keep=boom)
        self.assertEqual(len(self.lease.pending("ab")), 1)  # not acked
        self.assertEqual([i["text"] for i in client.Agent("B").inbox()], ["keep me"])  # not dropped as a replay

    def test_question_survives_listen_stopping_mid_answer(self):
        lis = self.listener(cmd=stub(self.tmp, "print('x')"))
        lis.jobs.put = lambda it: None  # the process "dies" before the worker gets to it
        client.Agent("A").ask("B", "launch date?", wait=0)
        client.Agent("B")._fetch(0, keep=lis.handle)
        self.assertEqual(self.lease.pending("ab"), [])  # acked on the relay ...
        self.listener(cmd=stub(self.tmp, "print('x')"))  # ... and the next listen start hands it to the owner
        held = client.Agent("B").inbox()
        self.assertEqual([(i["type"], i["text"]) for i in held], [("ask", "launch date?")])
        self.assertIn("listen stopped", held[0]["auto_reply"])

    def test_answer_that_cannot_be_sent_is_kept(self):
        lis = self.listener(cmd=stub(self.tmp, "sys.stdin.read(); print(42)"))
        client.Agent("A").ask("B", "q?", wait=0)
        self.lease.fail_send = True
        with mock.patch.object(listen, "RESEND_AFTER", 0):
            client.Agent("B")._fetch(0, keep=lis.handle)
            lis.jobs.join()
        held = client.Agent("B").inbox()
        self.assertEqual(len(held), 1)
        self.assertIn("couldn't be sent", held[0]["auto_reply"])
        self.assertFalse(os.path.exists(profiles._pend_path("b")))

    def test_listener_answers_a_question_that_ask_picked_up(self):
        c = self.add("C", "ac")
        lis = self.listener(cmd=stub(self.tmp, "sys.stdin.read(); print('from B')"))
        client.Agent("C").ask("B", "hello B?", wait=0)
        r = client.Agent("B").ask("A", "anyone?", wait=1, poll=1)  # B's own ask reads C's question
        self.assertEqual(r["status"], "no_answer")
        lis.take_held()
        lis.jobs.join()
        ans = [i for i in client.Agent("C").inbox() if i["type"] == "answer"]
        self.assertEqual([i["text"] for i in ans], ["from B"])
        self.assertEqual(lis.take_held(), [])  # handled once

    def test_switch_off_held_ask_is_not_retaken(self):
        self.relay.answers_own["ab"] = False
        lis = self.listener(cmd=stub(self.tmp, "print('x')"))
        client.Agent("A").ask("B", "q", wait=0)
        lis.ag._fetch(0, keep=lis.handle)
        lis.jobs.join()
        self.assertEqual(lis.take_held(), [])
        self.assertEqual(len(client.Agent("B").inbox()), 1)


if __name__ == "__main__":
    unittest.main()
