"""Switchboard client: talks to the relay, keeps keys local.

Every agent on this computer has its own profile (see profiles.py). Messages use
RFC 9180 HPKE + Ed25519 sender signatures (crypto1). The old home-built v0
encryption is retired: it is never sent and never accepted.

What the relay can and cannot do: it hands out peers' public keys the first
time this agent sees them. After that the key is remembered (profiles pins), and
a peer whose key changes or disappears is refused. Replies to outside A2A
callers are sealed to the key this agent verified when it read their message,
never to a key the relay names later.
"""
import base64
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request

from . import __version__
from . import crypto1, profiles
from .profiles import ProfileError

DEFAULT_HOST = "https://switchboard.pioneerdigital.org"
MAX_AVATAR = 64 * 1024
UPGRADE = "uvx switchboard-agent@latest"

BLOCKED = ("BLOCKED: this environment can't reach %s (%s). Your user's invite code was NOT used. "
           "Tell your user, in one line, to rerun this on their own computer: Claude app = click 'Add folder'; "
           "ChatGPT = accept Work mode or open a local folder; otherwise any terminal on their machine. Then run the same commands again.")

REMOVED = "%s was removed from %s's Switchboard by the owner. Local keys deleted. Nothing else to uninstall."
NOT_E2E = "not end-to-end encrypted"
OLD_PEER = ("%s is on an old Switchboard version whose encryption is no longer supported, so nothing was sent. "
            "It must upgrade first: its owner runs `" + UPGRADE + "` there, or re-adds it from the dashboard.")
OLD_MSG = "sent with the old Switchboard encryption, which is no longer accepted. Ask the sender to upgrade (" + UPGRADE + ")."
KEY_CHANGED = ("%s's encryption key is not the one this agent first saw, so Switchboard refused to %s it. "
               "If %s was re-added on purpose, the owner should remove it and add it again from the dashboard; "
               "otherwise treat it as a possible impersonation.")
OLD_SERVER = ("This Switchboard server doesn't support switchboard-agent %s yet (no standard-encryption keys). "
              "Try again later, or tell the network owner.") % __version__
KEY_CONFLICT = ("The server holds a different encryption key for this agent than this computer does, so it can't "
                "send or read messages. Ask the owner to remove it on the dashboard, then join again.")
_CTRL = re.compile(u"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def clean(s, n=60):
    """A sender-supplied name or id, safe to put on one header line."""
    return _CTRL.sub(" ", str(s if s is not None else ""))[:n].strip() or "?"


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
        d.setdefault("http", e.code)
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
    if not host and link.startswith(("https://", "http://localhost:", "http://127.0.0.1:")) and "/j/" in link:
        host = link.split("/j/")[0]
    host = (host or os.environ.get("SWITCHBOARD_HOST") or DEFAULT_HOST).rstrip("/")
    priv1, pub1 = crypto1.new_identity()
    body = {"code": normalize_code(code), "name": name, "kind": kind, "pub1": pub1,
            "proof1": crypto1.join_proof(priv1, pub1), "client": "switchboard-agent/" + __version__}
    try:
        r = api(host, "POST", "/api/join", body)
    except SwitchboardError as e:
        if str(e) == "Missing or invalid public key.":  # a server from before 0.4
            raise SwitchboardError(OLD_SERVER)
        raise
    slug = profiles.new_slug(r["name"])
    p = {"slug": slug, "host": host, "name": r["name"], "asked_name": name, "kind": kind, "id": r["agent_id"],
         "token": r["token"], "owner": r.get("owner"), "priv1": priv1, "pub1": pub1,
         "pub1_published": r.get("pub1") == pub1, "created": int(time.time())}
    profiles.save(p)
    return {"name": p["name"], "owner": p["owner"], "fingerprint": _fp(pub1), "slug": slug,
            "approved": r.get("approved", True)}


class Agent:
    def __init__(self, as_name=None, upgrade=True):
        try:
            self.c = profiles.choose(as_name)
        except ProfileError as e:
            raise SwitchboardError(str(e), e.code)
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
        """First run after upgrading from 0.3.x: create standard keys and publish them in place.

        Locked and re-read from disk, so an MCP server and a CLI call starting at the same time
        end up with one key, on disk and on the server."""
        if self.c.get("priv1") and self.c.get("pub1_published"):
            return
        with profiles.locked(self.c["slug"]):
            try:
                self.c = profiles.load(self.c["slug"])
            except ProfileError as e:
                raise SwitchboardError(str(e))
            if not self.c.get("priv1"):
                self.c["priv1"], self.c["pub1"] = crypto1.new_identity()
                self.c["pub1_published"] = False
                self._save()  # keep the private key before telling the server about it
            if self.c.get("pub1_published"):
                return
            try:
                r = self._api("POST", "/api/agent/rekey", {
                    "pub1": self.c["pub1"], "proof": crypto1.rekey_proof(self.c["priv1"], self.c["id"], self.c["pub1"])})
            except SwitchboardError as e:
                if e.code == "key_conflict":
                    raise SwitchboardError(KEY_CONFLICT, e.code, e.data)
                if e.data.get("http") == 404 and str(e) == "not found":
                    raise SwitchboardError(OLD_SERVER)
                raise
            if r.get("pub1") != self.c["pub1"]:
                raise SwitchboardError(OLD_SERVER)
            self.c["pub1_published"] = True
            self._save()

    # ---- identity ----
    def me(self):
        out = {"name": self.c["name"], "id": self.c["id"], "owner": self.c.get("owner"),
               "fingerprint": _fp(self.c["pub1"]), "host": self.c["host"], "slug": self.c["slug"]}
        out.update(self._api("GET", "/api/agent/me").get("agent", {}))
        return out

    def _all_peers(self):
        ps = self._api("GET", "/api/agent/peers")["agents"]
        fresh = {p["id"]: p["pub1"] for p in ps if p.get("id") != self.c["id"] and crypto1.valid_pub(p.get("pub1"))}
        self.pins = profiles.pins_add(self.c["slug"], fresh)  # remember keys on first sight
        return ps

    def peers(self):
        return [p for p in self._all_peers() if p["id"] != self.c["id"]]

    def peer_problem(self, p):
        """Why this peer can't be messaged, or None."""
        pinned = getattr(self, "pins", None)
        if pinned is None:
            pinned = profiles.pins_load(self.c["slug"])
        old = pinned.get(p["id"])
        if old and p.get("pub1") != old:
            return "key changed"
        if not old and not crypto1.valid_pub(p.get("pub1")):
            return "old version, must upgrade"
        return None

    def _peer_key(self, p, verb):
        pins = profiles.pins_load(self.c["slug"])
        old = pins.get(p["id"])
        if old:
            if p.get("pub1") != old:
                raise SwitchboardError(KEY_CHANGED % (clean(p["name"]), verb, clean(p["name"])), "key_changed")
            return old
        if not crypto1.valid_pub(p.get("pub1")):
            raise SwitchboardError(OLD_PEER % clean(p["name"]), "peer_needs_upgrade")
        profiles.pins_add(self.c["slug"], {p["id"]: p["pub1"]})
        return p["pub1"]

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
        key = self._peer_key(peer, "send to")
        obj = dict(obj, mid=secrets.token_hex(12), ts=time.time(), **{"from": self.c["id"], "to": peer["id"]})
        return crypto1.seal(self.c["priv1"], self.c["pub1"], key, obj)

    def _post(self, peer, blob):
        return self._api("POST", "/api/agent/send", {"to": peer["id"], "blob": blob}).get("status", "sent")

    def _send(self, peer, obj):
        return self._post(peer, self._seal(peer, obj))

    def send(self, name, text):
        p = self.find(name)
        return {"to": p["name"], "status": self._send(p, {"type": "text", "text": text}), "encryption": "v1"}

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
        items = [{"to": m["id"], "blob": self._seal(m, {"type": "text", "text": text, "group": group})} for m in uniq]
        r = self._api("POST", "/api/agent/send_group", {"group": gid, "items": items})
        st = {x["to"]: x["status"] for x in r.get("results", [])}
        return {"group": gid, "results": [{"to": m["name"], "status": st.get(m["id"], "sent")} for m in uniq]}

    def ping(self, name=None):
        targets = self.peers() if not name else [self.find(name)]
        out = []
        for p in targets:
            try:
                blob = self._seal(p, {"type": "ping", "text": "Hi %s, it's %s. Are we connected?" % (p["name"], self.c["name"])})
            except SwitchboardError as e:
                if name:
                    raise
                out.append({"to": p["name"], "status": "not sent: " + str(e)})
                continue
            out.append({"to": p["name"], "status": self._post(p, blob)})
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
        blob = m.get("blob") or ""
        if crypto1.blob_version(blob) != 1:
            raise crypto1.CryptoError(OLD_MSG)
        try:
            key = self._peer_key(p, "read a message from")
        except SwitchboardError as e:
            raise crypto1.CryptoError(str(e))
        o = crypto1.unseal(self.c["priv1"], self.c["pub1"], key, blob)
        if o.get("from") != p["id"] or o.get("to") != self.c["id"]:
            raise crypto1.CryptoError("message names a different sender or recipient")
        return o

    def _open_a2a(self, m):
        """Outside A2A message. Whether it is encrypted is decided by what we can verify, not by the relay."""
        a = m.get("a2a") or {}
        task = clean(a.get("task"), 40)
        item = {"from": "an outside A2A agent", "sent": m.get("sent"), "type": "a2a", "task": task}
        caller = a.get("caller_pub1")
        if a.get("caller_name"):
            item["caller_name"] = clean(a["caller_name"])
        rec = {"ts": time.time()}
        if m.get("blob"):
            item["encrypted"] = True
            if caller and crypto1.valid_pub(caller):
                item["from"] += " (key %s)" % _fp(caller)
            try:
                o = crypto1.unseal(self.c["priv1"], self.c["pub1"], caller, m["blob"])
            except (crypto1.CryptoError, KeyError, ValueError, TypeError) as e:
                item.update(type="unreadable", text="", note=str(e))
                rec.update(enc=True, bad=True)
                return item, {task: rec}
            item.update(text=str(o.get("text", ""))[:20000], label="end-to-end encrypted")
            rec.update(enc=True, caller_pub1=caller)
        else:
            item.update(encrypted=False, text=str(a.get("text", ""))[:20000], label=NOT_E2E)
            rec.update(enc=False)
        return item, {task: rec}

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
        out, a2a_recs = [], {}
        try:
            for m in msgs:
                if m.get("from") == "a2a":
                    item, rec = self._open_a2a(m)
                    a2a_recs.update(rec)
                    out.append(item)
                    continue
                p = by_id.get(m.get("from"))
                item = {"from": clean(p["name"]) if p else "a removed agent", "sent": m.get("sent")}
                if not p:
                    item.update(type="unreadable", text="", note="sender is no longer on the network")
                    out.append(item)
                    continue
                try:
                    o = self._open(p, m)
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
                item.update(type=clean(o.get("type", "text"), 12), text=str(o.get("text", ""))[:20000], encryption="v1")
                g = o.get("group")
                if isinstance(g, dict) and g.get("id"):
                    item["group"] = clean(g["id"], 40)
                    # names come from the relay's peer list by id, not from the sender's text
                    ids = [x.get("id") for x in g.get("members", []) if isinstance(x, dict)]
                    item["also_to"] = [clean(by_id[i]["name"]) if i in by_id else "an agent no longer on the network"
                                       for i in ids if i not in (self.c["id"], p["id"])]
                if item["type"] == "ping":
                    try:
                        self._send(p, {"type": "pong", "text": "%s here, we're connected ✓" % self.c["name"]})
                        item["auto_reply"] = "pong sent"
                    except SwitchboardError as e:
                        if e.code == "removed_by_owner":
                            raise
                        item["auto_reply"] = "pong failed (%s)" % e
                out.append(item)
        finally:
            profiles.seen_save(self.c["slug"], seen)
            profiles.a2a_record(self.c["slug"], a2a_recs)
        return out

    def reply(self, task, text, done=True):
        """Reply to an outside A2A agent's message (task id from the inbox).

        Uses only what this agent recorded when it read the message: an encrypted message gets a reply
        sealed to the caller key it was verified with; a plain one gets a plain reply."""
        rec = profiles.a2a_load(self.c["slug"]).get(task)
        if not rec:
            raise SwitchboardError("No message with task %s has been read by %s on this computer. Read it with inbox first."
                                   % (clean(task, 40), self.c["name"]))
        if rec.get("bad"):
            raise SwitchboardError("The message in task %s couldn't be decrypted, so there is nothing safe to reply to." % clean(task, 40))
        body = {"task": task, "done": bool(done)}
        if rec.get("enc"):
            obj = {"type": "text", "text": text, "task": task, "mid": secrets.token_hex(12), "ts": time.time()}
            body["blob"] = crypto1.seal(self.c["priv1"], self.c["pub1"], rec["caller_pub1"], obj)
        else:
            body["text"] = text
        self._api("POST", "/api/agent/a2a/reply", body)
        return {"task": task, "encrypted": bool(rec.get("enc"))}


def sniff_image(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None
