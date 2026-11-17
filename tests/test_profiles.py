import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from switchboard_agent import client, crypto0, crypto1, profiles


def fake(name, aid, host="https://x"):
    return {"name": name, "id": aid, "token": "u1.%s.s" % aid, "host": host, "owner": "Matt"}


class Profiles(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"SWITCHBOARD_HOME": self.d}, clear=False)
        self.env.start()
        os.environ.pop("SWITCHBOARD_AS", None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.d)

    def add(self, name, aid):
        slug = profiles.new_slug(name)
        p = dict(fake(name, aid), slug=slug)
        profiles.save(p)
        return p

    def test_permissions(self):
        p = self.add("Claude", "a1")
        self.assertEqual(stat.S_IMODE(os.stat(profiles.agents_dir()).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(profiles.path_for(p["slug"])).st_mode), 0o600)

    def test_single_profile_is_default(self):
        self.add("Claude", "a1")
        self.assertEqual(profiles.choose()["id"], "a1")

    def test_several_need_choice(self):
        self.add("Claude", "a1")
        self.add("Codex", "a2")
        with self.assertRaisesRegex(profiles.ProfileError, '"Claude", "Codex"'):
            profiles.choose()
        self.assertEqual(profiles.choose("codex")["id"], "a2")
        self.assertEqual(profiles.choose("CLAUDE")["id"], "a1")
        os.environ["SWITCHBOARD_AS"] = "Codex"
        self.assertEqual(profiles.choose()["id"], "a2")
        with self.assertRaisesRegex(profiles.ProfileError, "No agent named"):
            profiles.choose("Grok")

    def test_same_name_never_overwrites(self):
        a = self.add("Claude", "a1")
        b = self.add("Claude", "a2")
        self.assertNotEqual(a["slug"], b["slug"])
        self.assertEqual({p["id"] for p in profiles.all_profiles()}, {"a1", "a2"})

    def test_legacy_migration(self):
        os.makedirs(self.d, exist_ok=True)
        with open(profiles.legacy_path(), "w") as f:
            json.dump(dict(fake("Boo", "a9"), priv="ab", pub="cd"), f)
        p = profiles.choose()
        self.assertEqual(p["id"], "a9")
        self.assertEqual(p["priv"], "ab")
        self.assertFalse(os.path.exists(profiles.legacy_path()))
        self.assertTrue(os.path.exists(profiles.legacy_path() + ".migrated"))
        self.assertEqual(len(profiles.all_profiles()), 1)  # idempotent

    def test_delete_only_one(self):
        a = self.add("Claude", "a1")
        self.add("Codex", "a2")
        profiles.delete(a["slug"])
        self.assertEqual([p["id"] for p in profiles.all_profiles()], ["a2"])


class Upgrade(unittest.TestCase):
    """A 0.3.1 profile (v0 keys only) gets v1 keys, publishes them via rekey and keeps its identity."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"SWITCHBOARD_HOME": self.d})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.d)

    def test_rekey_in_place(self):
        priv, pub = crypto0.new_keypair()
        os.makedirs(self.d, exist_ok=True)
        with open(profiles.legacy_path(), "w") as f:
            json.dump(dict(fake("Claude", "a1"), priv="%x" % priv, pub="%x" % pub), f)
        calls = []

        def fake_api(host, method, path, body=None, token=None, timeout=40):
            calls.append((path, body))
            return {"ok": True}
        with mock.patch.object(client, "api", fake_api):
            ag = client.Agent()
        self.assertEqual(calls[0][0], "/api/agent/rekey")
        self.assertEqual(calls[0][1]["pub1"], ag.c["pub1"])
        self.assertEqual(ag.c["id"], "a1")
        self.assertEqual(ag.c["token"], "u1.a1.s")
        self.assertEqual(ag.c["priv"], "%x" % priv)  # v0 key kept to read old mail
        with mock.patch.object(client, "api", fake_api):
            client.Agent()
        self.assertEqual(len(calls), 1)  # only once

    def test_removed_by_owner_deletes_only_that_profile(self):
        for name, aid in (("Claude", "a1"), ("Codex", "a2")):
            slug = profiles.new_slug(name)
            p = dict(fake(name, aid), slug=slug, pub1_published=True)
            p["priv1"], p["pub1"] = crypto1.new_identity()
            profiles.save(p)

        def gone(*a, **k):
            raise client.SwitchboardError("Claude was removed", "removed_by_owner", {"owner": "Matt"})
        with mock.patch.object(client, "api", gone):
            ag = client.Agent("Claude")
            with self.assertRaises(client.SwitchboardError) as cm:
                ag.inbox()
        self.assertEqual(str(cm.exception),
                         "Claude was removed from Matt's Switchboard by the owner. Local keys deleted. Nothing else to uninstall.")
        self.assertEqual([p["name"] for p in profiles.all_profiles()], ["Codex"])


class Sealing(unittest.TestCase):
    """Client picks v1 for upgraded peers and v0 for legacy peers; reads both."""

    def mk(self, aid, legacy=False):
        priv, pub = crypto0.new_keypair()
        c = {"id": aid, "name": aid, "priv": "%x" % priv, "pub": "%x" % pub, "slug": aid}
        if not legacy:
            c["priv1"], c["pub1"] = crypto1.new_identity()
        a = client.Agent.__new__(client.Agent)
        a.c = c
        return a

    def peer(self, a):
        return {"id": a.c["id"], "name": a.c["name"], "pub": a.c["pub"], "pub1": a.c.get("pub1")}

    def test_version_selection(self):
        x, y, old = self.mk("x"), self.mk("y"), self.mk("old", legacy=True)
        blob, ver = x._seal(self.peer(y), {"type": "text", "text": "hi"})
        self.assertEqual(ver, 1)
        self.assertEqual(y._open(self.peer(x), {"blob": blob})[0]["text"], "hi")
        blob, ver = x._seal(self.peer(old), {"type": "text", "text": "hi"})
        self.assertEqual(ver, 0)
        o = crypto0.unseal(int(old.c["priv"], 16), int(old.c["pub"], 16), int(x.c["pub"], 16), blob)
        self.assertEqual(o["text"], "hi")
        legacy_blob = crypto0.seal(int(old.c["priv"], 16), int(old.c["pub"], 16), int(x.c["pub"], 16), {"type": "ping", "text": "yo"})
        self.assertEqual(x._open(self.peer(old), {"blob": legacy_blob})[0]["text"], "yo")

    def test_redirected_ids_rejected(self):
        x, y = self.mk("x"), self.mk("y")
        blob, _ = x._seal(self.peer(y), {"type": "text", "text": "hi"})
        z = dict(self.peer(x), id="z")  # server claims a different sender id with x's key
        with self.assertRaises(crypto1.CryptoError):
            y._open(z, {"blob": blob})

    def test_sniff(self):
        self.assertEqual(client.sniff_image(b"\x89PNG\r\n\x1a\n...."), "image/png")
        self.assertEqual(client.sniff_image(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertEqual(client.sniff_image(b"RIFF\0\0\0\0WEBPVP8 "), "image/webp")
        self.assertIsNone(client.sniff_image(b"<svg xmlns="))


if __name__ == "__main__":
    unittest.main()
