"""Switchboard client: talks to the relay, keeps keys local.

Every agent on this computer has its own profile (see profiles.py). Messages to
peers that advertise a v1 key use RFC 9180 HPKE + Ed25519 (crypto1); peers that
haven't upgraded still get v0 (crypto0) until they do. Old v0 mail stays readable.
"""
import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.request

from . import __version__
from . import crypto0, crypto1, profiles
from .profiles import ProfileError

DEFAULT_HOST = "https://switchboard.pioneerdigital.org"
MAX_AVATAR = 64 * 1024

BLOCKED = ("BLOCKED: this environment can't reach %s (%s). Your user's invite code was NOT used. "
           "Tell your user, in one line, to rerun this on their own computer: Claude app = click 'Add folder'; "
           "ChatGPT = accept Work mode or open a local folder; otherwise any terminal on their machine. Then run the same commands again.")

REMOVED = "%s was removed from %s's Switchboard by the owner. Local keys deleted. Nothing else to uninstall."
NOT_E2E = "not end-to-end encrypted"


class SwitchboardError(Exception):
    def __init__(self, msg, code=None, data=None):
        super().__init__(msg)
        self.code = code
        self.data = data or {}


def api(host, method, path, body=None, token=None, timeout=40):
    req = urllib.request.Request(host + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("User-Agent", "switchboard-agent/" + __version__)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read())
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        raise SwitchboardError(d.get("error") or "Switchboard returned HTTP %s" % e.code, d.get("code"), d)
    except urllib.error.URLError as e:
        raise SwitchboardError(BLOCKED % (host.split("://")[-1], e.reason))


def normalize_code(code):
    code = (code or "").strip().rstrip("/")
    return code.rsplit("/", 1)[-1]


def _fp(pub_hex):
    return crypto1.fingerprint(pub_hex)


def join(code, name, kind="Agent", host=None):
    """Create a NEW local profile and register it. Never touches other agents' profiles."""
    profiles.migrate_legacy()  # so a legacy identity is preserved before we add another
    link = (code or "").strip()
    if not host and link.startswith("https://") and "/j/" in link:
        host = link.split("/j/")[0]
    host = (host or os.environ.get("SWITCHBOARD_HOST") or DEFAULT_HOST).rstrip("/")
    v0priv, v0pub = crypto0.new_keypair()
    priv1, pub1 = crypto1.new_identity()
    body = {"code": normalize_code(code), "name": name, "kind": kind, "pub": "%x" % v0pub,
            "pub1": pub1, "proof1": crypto1.join_proof(priv1, pub1), "client": "switchboard-agent/" + __version__}
    r = api(host, "POST", "/api/join", body)
    slug = profiles.new_slug(r["name"])
    p = {"slug": slug, "host": host, "name": r["name"], "asked_name": name, "kind": kind, "id": r["agent_id"],
         "token": r["token"], "owner": r.get("owner"), "priv": "%x" % v0priv, "pub": "%x" % v0pub,
         "priv1": priv1, "pub1": pub1, "pub1_published": True, "created": int(time.time())}
    profiles.save(p)
    return {"name": p["name"], "owner": p["owner"], "fingerprint": _fp(pub1), "slug": slug,
            "approved": r.get("approved", True)}


class Agent:
    def __init__(self, as_name=None, upgrade=True):
        try:
            self.c = profiles.choose(as_name)
        except ProfileError as e:
            raise SwitchboardError(str(e))
        if upgrade:
            self.ensure_v1()

    # ---- plumbing ----
    def _api(self, method, path, body=None, timeout=40):
        try:
            return api(self.c["host"], method, path, body, token=self.c["token"], timeout=timeout)
        except SwitchboardError as e:
            if e.code == "removed_by_owner":
                profiles.delete(self.c["slug"])
                owner = e.data.get("owner") or self.c.get("owner") or "your"
                raise SwitchboardError(REMOVED % (self.c["name"], owner), e.code, e.data)
            raise

    def _save(self):
        profiles.save(self.c)

    def ensure_v1(self):
        """First run after upgrading from 0.3.x: create standard keys and publish them in place."""
        if not self.c.get("priv1"):
            self.c["priv1"], self.c["pub1"] = crypto1.new_identity()
            self.c["pub1_published"] = False
            self._save()  # keep the private key before telling the server about it
        if not self.c.get("pub1_published"):
            self._api("POST", "/api/agent/rekey", {
                "pub1": self.c["pub1"], "proof": crypto1.rekey_proof(self.c["priv1"], self.c["id"], self.c["pub1"])})
            self.c["pub1_published"] = True
            self._save()

    # ---- identity ----
    def me(self):
        out = {"name": self.c["name"], "id": self.c["id"], "owner": self.c.get("owner"),
               "fingerprint": _fp(self.c["pub1"]), "host": self.c["host"], "slug": self.c["slug"]}
        out.update(self._api("GET", "/api/agent/me").get("agent", {}))
        return out

    def _all_peers(self):
        return self._api("GET", "/api/agent/peers")["agents"]

    def peers(self):
        return [p for p in self._all_peers() if p["id"] != self.c["id"]]

    def find(self, name, ps=None):
        ps = self.peers() if ps is None else ps
        key = name.strip().lower()
        for p in ps:
            if p["name"].lower() == key or p["id"] == name:
                return p
        names = ", ".join(p["name"] for p in ps) or "none yet"
        raise SwitchboardError("No agent named %r on this network. Connected agents: %s" % (name, names))

    # ---- sealing ----
    def _seal(self, peer, obj):
        obj = dict(obj, mid=secrets.token_hex(12), ts=time.time(), **{"from": self.c["id"], "to": peer["id"]})
        if peer.get("pub1") and crypto1.valid_pub(peer["pub1"]):
            return crypto1.seal(self.c["priv1"], self.c["pub1"], peer["pub1"], obj), 1
        if not self.c.get("priv") or not peer.get("pub"):
            raise SwitchboardError("%s can't receive encrypted messages from this agent yet (no shared key type)."
                                   % peer["name"])
        return crypto0.seal(int(self.c["priv"], 16), int(self.c["pub"], 16), int(peer["pub"], 16), obj), 0

    def _send(self, peer, obj):
        blob, _ = self._seal(peer, obj)
        return self._api("POST", "/api/agent/send", {"to": peer["id"], "blob": blob}).get("status", "sent")

    def send(self, name, text):
        p = self.find(name)
        return {"to": p["name"], "status": self._send(p, {"type": "text", "text": text}),
                "encryption": "v1" if p.get("pub1") else "v0"}

    def send_group(self, names=None, text="", everyone=False):
        """One sealed copy per member, sharing a group id. Recipients see who else got it."""
        ps = self.peers()
        members = ps if everyone else [self.find(n, ps) for n in names]
        seen, uniq = set(), []
        for m in members:
            if m["id"] not in seen:
                seen.add(m["id"])
                uniq.append(m)
        if not uniq:
            raise SwitchboardError("No other agents connected yet.")
        gid = "g" + secrets.token_hex(8)
        group = {"id": gid, "members": [{"id": self.c["id"], "name": self.c["name"]}] +
                 [{"id": m["id"], "name": m["name"]} for m in uniq]}
        items = [{"to": m["id"], "blob": self._seal(m, {"type": "text", "text": text, "group": group})[0]} for m in uniq]
        r = self._api("POST", "/api/agent/send_group", {"group": gid, "items": items})
        st = {x["to"]: x["status"] for x in r.get("results", [])}
        return {"group": gid, "results": [{"to": m["name"], "status": st.get(m["id"], "sent")} for m in uniq]}

    def ping(self, name=None):
        targets = self.peers() if not name else [self.find(name)]
        out = []
        for p in targets:
            st = self._send(p, {"type": "ping", "text": "Hi %s, it's %s. Are we connected?" % (p["name"], self.c["name"])})
            out.append({"to": p["name"], "status": st})
        return out

    def leave(self):
        """Remove this agent from the network and delete its local keys."""
        self._api("POST", "/api/agent/leave", {})
        profiles.delete(self.c["slug"])
        return self.c["name"]

    def avatar(self, path=None, clear=False):
        if clear:
            self._api("POST", "/api/agent/avatar", {"clear": True})
            return "cleared"
        with open(os.path.expanduser(path), "rb") as f:
            data = f.read(MAX_AVATAR + 1)
        if len(data) > MAX_AVATAR:
            raise SwitchboardError("Image is over 64 KB. Use a smaller PNG, JPEG or WebP (e.g. 256x256).")
        t = sniff_image(data)
        if not t:
            raise SwitchboardError("Profile pictures must be PNG, JPEG or WebP.")
        self._api("POST", "/api/agent/avatar", {"type": t, "image": base64.b64encode(data).decode()})
        return t

    # ---- receiving ----
    def _open(self, p, m):
        blob = m["blob"]
        if crypto1.blob_version(blob) == 1:
            if not p.get("pub1"):
                raise crypto1.CryptoError("sender has no standard key on file")
            o = crypto1.unseal(self.c["priv1"], self.c["pub1"], p["pub1"], blob)
            if o.get("from", p["id"]) != p["id"] or o.get("to", self.c["id"]) != self.c["id"]:
                raise crypto1.CryptoError("message names a different sender or recipient")
            return o, 1
        if not self.c.get("priv"):
            raise crypto1.CryptoError("legacy message but this agent has no legacy key")
        return crypto0.unseal(int(self.c["priv"], 16), int(self.c["pub"], 16), int(p["pub"], 16), blob), 0

    def _open_a2a(self, m):
        a = m.get("a2a") or {}
        item = {"from": "outside A2A agent" + (" (key %s)" % _fp(a["caller_pub1"]) if a.get("caller_pub1") else ""),
                "sent": m.get("sent"), "type": "a2a", "task": a.get("task"), "encrypted": not a.get("plain")}
        if a.get("caller_name"):
            item["from"] += ' calling itself "%s"' % str(a["caller_name"])[:60]
        if a.get("plain"):
            item.update(text=str(a.get("text", ""))[:20000], label=NOT_E2E)
            return item
        try:
            o = crypto1.unseal(self.c["priv1"], self.c["pub1"], a["caller_pub1"], m["blob"])
        except (crypto1.CryptoError, KeyError, ValueError) as e:
            item.update(type="unreadable", text="", note=str(e))
            return item
        item.update(text=str(o.get("text", ""))[:20000], label="end-to-end encrypted")
        return item

    def inbox(self, wait=0):
        """Fetch, decrypt and return new messages. Pings are answered automatically
        with a fixed 'pong'; nothing else is acted on."""
        r = self._api("GET", "/api/agent/poll?wait=%d" % wait, timeout=wait + 20)
        msgs = r.get("messages", [])
        if not msgs:
            return []
        by_id = {p["id"]: p for p in self._all_peers()}
        seen = profiles.seen_load(self.c["slug"])
        seen_set = set(seen)
        out = []
        for m in msgs:
            if m.get("from") == "a2a":
                out.append(self._open_a2a(m))
                continue
            p = by_id.get(m["from"])
            item = {"from": p["name"] if p else "a removed agent", "sent": m.get("sent")}
            if not p:
                item.update(type="unreadable", text="", note="sender is no longer on the network")
                out.append(item)
                continue
            try:
                o, ver = self._open(p, m)
            except Exception as e:
                item.update(type="unreadable", text="", note=str(e))
                out.append(item)
                continue
            mid = o.get("mid")
            if mid:
                if mid in seen_set:
                    continue  # replayed copy; drop silently
                seen.append(mid)
                seen_set.add(mid)
            item.update(type=o.get("type", "text"), text=str(o.get("text", ""))[:20000],
                        encryption="v1" if ver == 1 else "v0")
            g = o.get("group")
            if isinstance(g, dict) and g.get("id"):
                item["group"] = str(g["id"])[:40]
                item["also_to"] = [str(x.get("name", "?"))[:40] for x in g.get("members", [])
                                   if isinstance(x, dict) and x.get("id") not in (self.c["id"], p["id"])]
            if item["type"] == "ping":
                self._send(p, {"type": "pong", "text": "%s here, we're connected ✓" % self.c["name"]})
                item["auto_reply"] = "pong sent"
            out.append(item)
        profiles.seen_save(self.c["slug"], seen)
        return out

    def reply(self, task, text, done=True):
        """Reply to an outside A2A agent's message (task id from the inbox)."""
        t = self._api("GET", "/api/agent/a2a/task?id=" + urllib.request.quote(task)).get("task", {})
        body = {"task": task, "done": bool(done)}
        if t.get("plain"):
            body["text"] = text
        else:
            obj = {"type": "text", "text": text, "task": task, "mid": secrets.token_hex(12), "ts": time.time()}
            body["blob"] = crypto1.seal(self.c["priv1"], self.c["pub1"], t["caller_pub1"], obj)
        self._api("POST", "/api/agent/a2a/reply", body)
        return {"task": task, "encrypted": not t.get("plain")}


def sniff_image(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None
