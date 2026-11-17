"""Files between an owner's agents (0.7): encrypted chunks, safe names, no overwrite, size cap, rename sync."""
import hashlib
import io
import json
import os
import sys
from unittest import mock

from switchboard_agent import cli, client, files, mcp, profiles

from test_ask import Base


class FileLoop:
    """Loopback relay with the 0.7 file endpoints. Stores exactly what a real relay would: ciphertext chunks."""

    def __init__(self, base):
        self.base, self.files, self.chunks, self.done = base, {}, {}, set()
        self.self_name = {}

    def __call__(self, host, method, path, body=None, token=None, timeout=40, raw=None, raw_out=False):
        me = token.split(".")[1]
        if path.startswith("/api/agent/poll"):
            r = self.base(host, method, path, body, token, timeout)
            if me in self.self_name:
                r["self"]["name"] = self.self_name[me]
            return r
        if path == "/api/agent/me" and me in self.self_name:
            return {"agent": {"name": self.self_name[me]}}
        if path == "/api/agent/file/new":
            if body["size"] > files.MAX_FILE:
                raise client.SwitchboardError("too big", "file_too_big", {"http": 413})
            fid = "f" + os.urandom(12).hex()
            self.files[fid] = dict(body, frm=me, n=files.parts(body["size"]), sent=False)
            return {"id": fid, "n": self.files[fid]["n"], "chunk": files.CHUNK}
        if path.startswith("/api/agent/file/chunk"):
            q = dict(x.split("=") for x in path.split("?")[1].split("&"))
            f = self.files.get(q["id"])
            if method == "PUT":
                assert f["frm"] == me and not f["sent"]
                self.chunks[(q["id"], int(q["i"]))] = bytes(raw)
                return {"ok": True}
            if not f or f["to"] != me or q["id"] in self.done:
                raise client.SwitchboardError("gone", "file_gone", {"http": 404})
            return self.chunks[(q["id"], int(q["i"]))]
        if path == "/api/agent/file/send":
            for fid in body["files"]:
                assert all((fid, i) in self.chunks for i in range(self.files[fid]["n"]))
                self.files[fid]["sent"] = True
            return self.base(host, method, "/api/agent/send", {"to": body["to"], "blob": body["blob"]}, token, timeout)
        if path == "/api/agent/file/done":
            self.done.add(body["id"])
            return {"ok": True}
        return self.base(host, method, path, body, token, timeout)


class Files(Base):
    def setUp(self):
        super().setUp()
        self.loop = FileLoop(self.relay)
        self.patch.stop()
        self.patch = mock.patch.object(client, "api", self.loop)
        self.patch.start()

    def mk(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_round_trip_both_ways_bytes_match(self):
        data = os.urandom(files.CHUNK * 2 + 12345)  # three chunks, last one short
        r = client.Agent("A").send_files("B", [self.mk("report.pdf", data)], "here you go")
        self.assertEqual(r["files"], [{"name": "report.pdf", "size": len(data)}])
        items = client.Agent("B").inbox()
        self.assertEqual(items[0]["type"], "file")
        self.assertEqual(items[0]["text"], "here you go")
        path = items[0]["files"][0]["path"]
        self.assertEqual(os.path.dirname(path), profiles.files_dir("b"))
        self.assertEqual(hashlib.sha256(open(path, "rb").read()).hexdigest(), hashlib.sha256(data).hexdigest())
        back = os.urandom(777)
        client.Agent("B").send_files("A", [self.mk("reply.txt", back)])
        got = client.Agent("A").inbox()[0]["files"][0]["path"]
        self.assertEqual(open(got, "rb").read(), back)
        self.assertEqual(len(self.loop.done), 2)  # both relay copies deleted after saving

    def test_relay_holds_only_ciphertext(self):
        data = b"TOP-SECRET-CANARY " * 5000
        client.Agent("A").send_files("B", [self.mk("plan.txt", data)])
        blob = b"".join(self.loop.chunks.values())
        self.assertNotIn(b"TOP-SECRET-CANARY", blob)
        self.assertEqual(len(blob), len(data) + 16)
        sent = self.relay.box["ab"][0]["blob"]
        self.assertNotIn("TOP-SECRET", sent)
        self.assertNotIn("plan.txt", sent)  # the manifest (name, key) travels sealed

    def test_empty_file(self):
        client.Agent("A").send_files("B", [self.mk("empty.txt", b"")])
        p = client.Agent("B").inbox()[0]["files"][0]["path"]
        self.assertEqual(os.path.getsize(p), 0)

    def test_path_traversal_and_hidden_names_neutralized(self):
        for bad, want in [("../../etc/passwd", "passwd"), ("..\\..\\boot.ini", "boot.ini"), (".bashrc", "bashrc"),
                          ("/abs/x.sh", "x.sh"), ("..", "file"), ("", "file"), ("a\nb\x00c.txt", "a_b_c.txt"),
                          ("CON", "_CON"), ("ok\u202egnp.exe", "ok_gnp.exe"), ("x" * 300 + ".pdf", "x" * 116 + ".pdf")]:
            self.assertEqual(files.safe_name(bad), want, bad)
        # a sender that lies about the name in the sealed manifest still can't escape the folder
        orig = client.Agent._seal

        def evil(agent, peer, obj):
            for f in obj.get("files") or []:
                f["name"] = "../../../escape.txt"
            return orig(agent, peer, obj)
        with mock.patch.object(client.Agent, "_seal", evil):
            client.Agent("A").send_files("B", [self.mk("fine.txt", b"x")])
        p = client.Agent("B").inbox()[0]["files"][0]["path"]
        self.assertEqual(p, os.path.join(profiles.files_dir("b"), "escape.txt"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "escape.txt")))

    def test_no_overwrite_adds_suffix(self):
        for body in (b"one", b"two", b"three"):
            client.Agent("A").send_files("B", [self.mk("same.txt", body)])
        paths = [x["files"][0]["path"] for x in client.Agent("B").inbox()]
        self.assertEqual([os.path.basename(p) for p in paths], ["same.txt", "same (2).txt", "same (3).txt"])
        self.assertEqual([open(p, "rb").read() for p in paths], [b"one", b"two", b"three"])

    def test_save_to(self):
        client.Agent("A").send_files("B", [self.mk("r.txt", b"hi")])
        b = client.Agent("B")
        b.save_to = os.path.join(self.tmp, "out")
        self.assertEqual(b.inbox()[0]["files"][0]["path"], os.path.join(self.tmp, "out", "r.txt"))

    def test_over_cap_refused_before_upload(self):
        big = self.mk("big.bin", b"")
        with open(big, "r+b") as f:
            f.truncate(files.MAX_FILE + 1)
        with self.assertRaises(client.SwitchboardError) as e:
            client.Agent("A").send_files("B", [big])
        self.assertIn("big.bin is 25.0 MB. Files can be up to 25.0 MB each.", str(e.exception))
        self.assertEqual(self.loop.files, {})

    def test_tampered_chunk_is_rejected_and_kept_for_retry(self):
        client.Agent("A").send_files("B", [self.mk("t.txt", b"hello world")])
        k = next(iter(self.loop.chunks))
        good = self.loop.chunks[k]
        self.loop.chunks[k] = good[:-1] + bytes([good[-1] ^ 1])
        it = client.Agent("B").inbox()[0]["files"][0]
        self.assertIn("tampered", it["error"])
        self.assertEqual(os.listdir(profiles.files_dir("b")), [])  # no partial file left
        self.loop.chunks[k] = good
        rows = client.Agent("B").files()
        self.assertEqual(rows[0]["status"], "saved")
        self.assertEqual(open(rows[0]["path"], "rb").read(), b"hello world")
        self.assertNotIn("key", json.dumps(profiles.files_load("b")).replace('"key": null', ""))

    def test_bad_manifest_is_unreadable_not_a_crash(self):
        orig = client.Agent._seal

        def evil(agent, peer, obj):
            if obj.get("files"):
                obj["files"][0]["size"] = -5
            return orig(agent, peer, obj)
        with mock.patch.object(client.Agent, "_seal", evil):
            client.Agent("A").send_files("B", [self.mk("x.txt", b"x")])
        it = client.Agent("B").inbox()[0]
        self.assertEqual(it["type"], "unreadable")

    def test_cli_send_file_and_files(self):
        p = self.mk("notes.md", b"# hi")
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            cli.main(["send", "B", "--file", p, "see attached", "--as", "A"])
        self.assertIn("Sent notes.md (4 bytes) to B", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            cli.main(["inbox", "--as", "B"])
        self.assertIn("see attached", out.getvalue())
        self.assertIn("[file saved: notes.md (4 bytes) -> ", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            cli.main(["files", "--as", "B"])
        self.assertIn("notes.md (4 bytes) from A: ", out.getvalue())

    def test_mcp_send_file(self):
        s = mcp.Server("A")
        txt = s.call("switchboard_send_file", {"name": "B", "paths": [self.mk("m.txt", b"abc")], "note": "n"})
        self.assertIn("Sent m.txt (3 bytes) to B", txt)
        txt = mcp.Server("B").call("switchboard_inbox", {})
        self.assertIn("[file saved: m.txt (3 bytes) -> ", txt)
        self.assertIn("1 file", txt)

    def test_rename_from_dashboard_updates_profile(self):
        self.loop.self_name["ab"] = "Bee"
        client.Agent("B").inbox()
        self.assertEqual(profiles.load("b")["name"], "Bee")
        self.assertEqual(client.Agent("B").c["name"], "Bee")  # --as the old name still finds it
        self.assertEqual(client.Agent("Bee").c["slug"], "b")
