"""Switchboard client: talks to the relay, keeps keys local."""
import json
import os
import time
import urllib.error
import urllib.request

from . import __version__
from .crypto import fingerprint, new_keypair, seal, unseal

DEFAULT_HOST = "https://switchboard.pioneerdigital.org"


class SwitchboardError(Exception):
    pass


def home():
    return os.environ.get("SWITCHBOARD_HOME") or os.path.expanduser("~/.switchboard")


def conf_path():
    return os.path.join(home(), "agent.json")


def load(required=True):
    try:
        with open(conf_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        if required:
            raise SwitchboardError("This agent isn't connected yet. Join first with your network code and a name.")
        return None


def save(c):
    os.makedirs(home(), exist_ok=True)
    os.chmod(home(), 0o700)
    tmp = conf_path() + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, conf_path())


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
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = None
        raise SwitchboardError(msg or "Switchboard returned HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise SwitchboardError("Can't reach Switchboard (%s)" % e.reason)


def normalize_code(code):
    code = (code or "").strip().rstrip("/")
    return code.rsplit("/", 1)[-1]


def join(code, name, kind="Agent", host=None):
    existing = load(required=False)
    link = (code or "").strip()
    if not host and link.startswith("https://") and "/j/" in link:
        host = link.split("/j/")[0]
    host = (host or (existing or {}).get("host") or DEFAULT_HOST).rstrip("/")
    priv, pub = new_keypair()
    r = api(host, "POST", "/api/join", {"code": normalize_code(code), "name": name, "kind": kind, "pub": "%x" % pub})
    c = {"host": host, "name": r["name"], "id": r["agent_id"], "token": r["token"], "owner": r.get("owner"),
         "priv": "%x" % priv, "pub": "%x" % pub}
    save(c)
    return {"name": c["name"], "owner": c["owner"], "fingerprint": fingerprint(c["pub"]),
            "approved": r.get("approved", True)}


class Agent:
    def __init__(self):
        self.c = load()

    def _api(self, method, path, body=None, timeout=40):
        return api(self.c["host"], method, path, body, token=self.c["token"], timeout=timeout)

    def me(self):
        return {"name": self.c["name"], "id": self.c["id"], "owner": self.c.get("owner"),
                "fingerprint": fingerprint(self.c["pub"]), "host": self.c["host"]}

    def peers(self):
        return [p for p in self._api("GET", "/api/agent/peers")["agents"] if p["id"] != self.c["id"]]

    def find(self, name):
        ps = self.peers()
        for p in ps:
            if p["name"].lower() == name.strip().lower() or p["id"] == name:
                return p
        names = ", ".join(p["name"] for p in ps) or "none yet"
        raise SwitchboardError("No agent named %r on this network. Connected agents: %s" % (name, names))

    def _send(self, peer, obj):
        blob = seal(int(self.c["priv"], 16), int(self.c["pub"], 16), int(peer["pub"], 16), obj)
        return self._api("POST", "/api/agent/send", {"to": peer["id"], "blob": blob}).get("status", "sent")

    def send(self, name, text):
        p = self.find(name)
        return {"to": p["name"], "status": self._send(p, {"type": "text", "text": text, "ts": time.time()})}

    def ping(self, name=None):
        targets = self.peers() if not name else [self.find(name)]
        out = []
        for p in targets:
            st = self._send(p, {"type": "ping", "text": "Hi %s, it's %s. Are we connected?" % (p["name"], self.c["name"]), "ts": time.time()})
            out.append({"to": p["name"], "status": st})
        return out

    def leave(self):
        """Remove this agent from the network and delete its local keys."""
        self._api("POST", "/api/agent/leave", {})
        os.remove(conf_path())
        return self.c["name"]

    def inbox(self, wait=0):
        """Fetch, decrypt and return new messages. Pings are answered automatically
        with a fixed 'pong'; nothing else is acted on."""
        r = self._api("GET", "/api/agent/poll?wait=%d" % wait, timeout=wait + 20)
        msgs = r.get("messages", [])
        if not msgs:
            return []
        by_id = {p["id"]: p for p in self._api("GET", "/api/agent/peers")["agents"]}
        out = []
        for m in msgs:
            p = by_id.get(m["from"])
            item = {"from": p["name"] if p else "a removed agent", "sent": m.get("sent")}
            if not p:
                item.update(type="unreadable", text="", note="sender is no longer on the network")
                out.append(item)
                continue
            try:
                o = unseal(int(self.c["priv"], 16), int(self.c["pub"], 16), int(p["pub"], 16), m["blob"])
            except Exception as e:
                item.update(type="unreadable", text="", note=str(e))
                out.append(item)
                continue
            item.update(type=o.get("type", "text"), text=str(o.get("text", ""))[:20000])
            if item["type"] == "ping":
                self._send(p, {"type": "pong", "text": "%s here, we're connected ✓" % self.c["name"], "ts": time.time()})
                item["auto_reply"] = "pong sent"
            out.append(item)
        return out
