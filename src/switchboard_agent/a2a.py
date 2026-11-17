"""Minimal A2A (v1.0, JSON-RPC binding) caller for Switchboard agents with the A2A bridge on.

Used by `switchboard a2a-send` and by tests. Any A2A client can do the same:
fetch the Agent Card, and if it declares the Switchboard E2E extension, seal the
message to the advertised HPKE key; otherwise (only if the owner allowed it) send
plain text, which is NOT end-to-end encrypted.
"""
import json
import os
import secrets
import time
import urllib.error
import urllib.request

from . import __version__, crypto1, profiles

EXT = "https://switchboard.pioneerdigital.org/ext/e2e/v1"
MEDIA = "application/vnd.switchboard.e2e.v1+json"


class A2AError(Exception):
    def __init__(self, msg, code=None, data=None, http=None):
        super().__init__(msg)
        self.code, self.data, self.http = code, data, http


def _http(method, url, body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("User-Agent", "switchboard-agent-a2a/" + __version__)
    req.add_header("Content-Type", "application/json")
    req.add_header("A2A-Version", "1.0")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            raise A2AError("HTTP %s" % e.code, http=e.code)


def card_url(url):
    url = url.rstrip("/")
    if url.endswith("agent-card.json"):
        return url
    return url + "/.well-known/agent-card.json"


def fetch_card(url):
    st, card = _http("GET", card_url(url))
    if st != 200:
        raise A2AError("couldn't fetch the agent card (HTTP %s)" % st, http=st)
    return card


def endpoint(card):
    for i in card.get("supportedInterfaces", []):
        if i.get("protocolBinding") == "JSONRPC":
            return i["url"]
    raise A2AError("agent has no JSON-RPC interface")


def e2e_params(card):
    for x in card.get("capabilities", {}).get("extensions", []):
        if x.get("uri") == EXT:
            return x.get("params") or {}
    return None


def caller_identity():
    """A persistent caller key for this computer, so replies can be opened later."""
    path = os.path.join(profiles.home(), "a2a-caller.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        priv, pub = crypto1.new_identity()
        c = {"priv1": priv, "pub1": pub}
        profiles.write_json(path, c)
        return c


def rpc(url, method, params, ext=True):
    body = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": method, "params": params}
    st, r = _http("POST", url, body, {"A2A-Extensions": EXT} if ext else None)
    if "error" in r:
        e = r["error"]
        raise A2AError(e.get("message", "error"), e.get("code"), e.get("data"), st)
    return r["result"]


def send(url, text, plain=False, name=None, ident=None, return_immediately=True, context=None):
    card = fetch_card(url)
    ep = endpoint(card)
    params = e2e_params(card)
    msg = {"messageId": secrets.token_hex(16), "role": "ROLE_USER"}
    if context:
        msg["contextId"] = context
    if plain:
        msg["parts"] = [{"text": text, "mediaType": "text/plain"}]
    else:
        if not params:
            raise A2AError("this agent doesn't advertise the Switchboard encryption extension")
        ident = ident or caller_identity()
        obj = {"type": "text", "text": text, "mid": secrets.token_hex(12), "ts": time.time()}
        blob = crypto1.seal(ident["priv1"], ident["pub1"], params["pub1"], obj)
        msg["parts"] = [{"data": {"v": 1, "senderKey": ident["pub1"], "blob": blob}, "mediaType": MEDIA}]
        msg["extensions"] = [EXT]
    if name:
        msg["metadata"] = {EXT: {"senderName": name}}
    res = rpc(ep, "SendMessage", {"message": msg, "configuration": {"returnImmediately": bool(return_immediately)}},
              ext=not plain)
    return res.get("task") or res, card


def get_task(url, task_id, card=None):
    card = card or fetch_card(url)
    return rpc(endpoint(card), "GetTask", {"id": task_id}).get("task") or {}


def read_reply(task, card, ident=None):
    """Return the reply text from a completed task (decrypting if needed), or None if not answered yet."""
    arts = task.get("artifacts") or []
    if not arts:
        return None
    part = (arts[-1].get("parts") or [{}])[0]
    if "text" in part:
        return {"text": part["text"], "encrypted": False}
    d = part.get("data") or {}
    params = e2e_params(card) or {}
    if d.get("senderKey") != params.get("pub1"):
        raise A2AError("reply was not sealed by the key this agent's card advertises")
    ident = ident or caller_identity()
    o = crypto1.unseal(ident["priv1"], ident["pub1"], d["senderKey"], d["blob"])
    return {"text": o.get("text", ""), "encrypted": True}


def wait_reply(url, task_id, card, seconds=60, ident=None):
    end = time.time() + seconds
    while True:
        t = get_task(url, task_id, card)
        r = read_reply(t, card, ident)
        if r is not None or time.time() > end:
            return t, r
        time.sleep(2)
