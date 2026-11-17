"""Local agent profiles: one file per agent, so several agents can share one computer.

    ~/.switchboard/                 (0700; override with SWITCHBOARD_HOME)
        agents/<slug>.json          (0600) one per agent: host, id, token, keys
        agents/<slug>.seen          recent message ids (replay protection)
        agents/<slug>.pins          peer id -> v1 key, remembered the first time each peer is seen
        agents/<slug>.a2a           outside A2A messages this agent read (task id -> how to reply)
        agent.json                  legacy single-agent file (<= 0.3.x); migrated automatically

Joining again under the same name on the same network replaces that agent's old, dead profile
(see dead_same_name) instead of adding "<name>-2".

Pick an agent with `--as NAME` or SWITCHBOARD_AS. If exactly one profile exists it is used.
"""
import contextlib
import json
import os
import re
import sys
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


class ProfileError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def home():
    return os.environ.get("SWITCHBOARD_HOME") or os.path.expanduser("~/.switchboard")


def agents_dir():
    return os.path.join(home(), "agents")


def legacy_path():
    return os.path.join(home(), "agent.json")


def _ensure_dirs():
    for d in (home(), agents_dir()):
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)


@contextlib.contextmanager
def locked(name):
    """Exclusive lock shared by every switchboard process on this computer (CLI calls, MCP servers)."""
    _ensure_dirs()
    fd = os.open(os.path.join(agents_dir(), "." + name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        yield
    finally:
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40]
    return s or "agent"


def path_for(slug):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", slug or ""):
        raise ProfileError("bad profile name %r" % slug)
    return os.path.join(agents_dir(), slug + ".json")


def write_json(path, obj):
    _ensure_dirs()
    tmp = path + ".tmp%d" % os.getpid()
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, type(default)) else default
    except (FileNotFoundError, ValueError):
        return default


def save(p):
    write_json(path_for(p["slug"]), p)


def load(slug):
    p = _read_json(path_for(slug), {})
    if not p.get("token"):
        raise ProfileError("This agent's profile (%s) is gone from this computer." % slug, "gone")
    p["slug"] = slug
    return p


def new_slug(name):
    """A slug no other profile uses. Creation is exclusive, so two joins can't collide."""
    _ensure_dirs()
    base = slugify(name)
    i = 1
    while True:
        slug = base if i == 1 else "%s-%d" % (base, i)
        try:
            fd = os.open(path_for(slug), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            i += 1
            continue
        os.close(fd)  # placeholder; caller fills it with save()
        return slug


def network_of(p):
    """The network (owner account) a profile belongs to: the part of its token before the first dot."""
    return str(p.get("token") or "").split(".", 1)[0]


def dead_same_name(host, network, name):
    """Slug of a local profile the server has just proven dead, or None.

    The server never gives two agents on one network the same name (it adds " 2", " 3"...). So when it
    hands a new agent the name N, any local profile with the same host, same network and name N belongs
    to an agent that is no longer on that network."""
    key = (name or "").strip().lower()
    for p in all_profiles():
        if p.get("host") == host and network_of(p) == network and (p.get("name") or "").strip().lower() == key:
            return p["slug"]
    return None


def as_value(p, ps=None):
    """The shortest --as value that picks this profile: its display name, or its slug if the name is shared."""
    ps = all_profiles() if ps is None else ps
    if sum(1 for q in ps if (q.get("name") or "").lower() == (p.get("name") or "").lower()) == 1:
        return p["name"]
    return p["slug"]


def _scrub_legacy(lp, slug, c):
    """Replace the old agent.json with a note that holds no token or keys."""
    write_json(lp + ".migrated", {"migrated_to": path_for(slug), "id": c.get("id"), "name": c.get("name"),
                                  "host": c.get("host"), "note": "token and keys moved; this file holds no secrets"})
    os.remove(lp)


def delete(slug):
    try:
        gone_id = _read_json(path_for(slug), {}).get("id")
    except ProfileError:
        gone_id = None
    for ext in (".json", ".seen", ".pins", ".a2a"):
        try:
            os.remove(os.path.join(agents_dir(), slug + ext))
        except FileNotFoundError:
            pass
    for lp in (legacy_path(), legacy_path() + ".migrated"):  # leftovers of the same agent
        if gone_id and _read_json(lp, {}).get("id") == gone_id:
            try:
                os.remove(lp)
            except FileNotFoundError:
                pass


def migrate_legacy():
    """Move a <=0.3.x ~/.switchboard/agent.json into agents/<slug>.json. Returns the slug or None.

    The legacy sb.py connector used the same file. Its encryption is retired, so its agent.json is
    moved over too (same id, name and token) and sb.py stops being able to use it."""
    lp = legacy_path()
    if not _read_json(lp, {}).get("token"):
        return None
    with locked("migrate"):
        c = _read_json(lp, {})  # re-read: another process may have migrated it meanwhile
        if not c.get("token"):
            return None
        for p in _load_all():
            if p.get("id") == c.get("id") and p.get("host") == c.get("host"):
                _scrub_legacy(lp, p["slug"], c)
                return p["slug"]
        slug = new_slug(c.get("name", "agent"))
        p = dict(c, slug=slug, asked_name=c.get("name"), migrated_from="agent.json")
        save(p)
        _scrub_legacy(lp, slug, c)
    sys.stderr.write("Switchboard: moved this agent's identity (\"%s\") to %s\n" % (p.get("name"), path_for(slug)))
    return slug


def _load_all():
    try:
        names = sorted(os.listdir(agents_dir()))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        if not n.endswith(".json") or n.startswith("."):
            continue
        p = _read_json(os.path.join(agents_dir(), n), {})
        if p.get("token"):
            p["slug"] = n[:-5]
            out.append(p)
    return out


def all_profiles():
    migrate_legacy()
    return _load_all()


def _label(p):
    return '"%s" (--as %s)' % (p["name"], p["slug"])


def choose(as_name=None):
    """Return the selected profile dict or raise ProfileError with a short, actionable message."""
    as_name = (as_name or os.environ.get("SWITCHBOARD_AS") or "").strip()
    ps = all_profiles()
    if not ps:
        raise ProfileError("This agent isn't connected yet. Join first with your network code and a name.", "none")
    if not as_name:
        if len(ps) == 1:
            return ps[0]
        raise ProfileError("Several agents are set up on this computer: %s. Pick one with --as NAME."
                           % ", ".join(_label(p) for p in ps), "ambiguous")
    key = as_name.lower()
    for test in (lambda p: p["slug"] == key, lambda p: p["name"].lower() == key, lambda p: p.get("id") == as_name,
                 lambda p: (p.get("mcp_as") or "").lower() == key,
                 lambda p: (p.get("asked_name") or "").lower() == key, lambda p: p["slug"] == slugify(as_name)):
        hits = [p for p in ps if test(p)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ProfileError("More than one agent here matches %r: %s. Use --as with the one you mean, e.g. --as %s."
                               % (as_name, ", ".join(_label(p) for p in hits), hits[0]["slug"]), "ambiguous")
    raise ProfileError("No agent named %r on this computer. Set up here: %s."
                       % (as_name, ", ".join(_label(p) for p in ps)), "nomatch")


# --- replay cache (message ids seen recently) ---
SEEN_MAX = 2000


def seen_load(slug):
    try:
        with open(os.path.join(agents_dir(), slug + ".seen")) as f:
            return [x for x in f.read().split() if x]
    except FileNotFoundError:
        return []


def seen_save(slug, ids):
    _ensure_dirs()
    path = os.path.join(agents_dir(), slug + ".seen")
    tmp = path + ".tmp%d" % os.getpid()
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(ids[-SEEN_MAX:]))
    os.replace(tmp, path)


# --- peer key pins (peer id -> pub1, first seen) ---
def pins_load(slug):
    return _read_json(os.path.join(agents_dir(), slug + ".pins"), {})


def pins_add(slug, new):
    """Remember keys for peers not pinned yet. Existing pins never change."""
    with locked(slug):
        path = os.path.join(agents_dir(), slug + ".pins")
        pins = _read_json(path, {})
        add = {k: v for k, v in new.items() if k not in pins}
        if add:
            pins.update(add)
            write_json(path, pins)
        return pins


# --- outside A2A messages this agent has read (so replies don't depend on the relay) ---
A2A_MAX = 500


def a2a_load(slug):
    return _read_json(os.path.join(agents_dir(), slug + ".a2a"), {})


def a2a_record(slug, recs):
    if not recs:
        return
    with locked(slug):
        path = os.path.join(agents_dir(), slug + ".a2a")
        d = _read_json(path, {})
        d.update(recs)
        if len(d) > A2A_MAX:
            d = dict(sorted(d.items(), key=lambda kv: kv[1].get("ts", 0))[-A2A_MAX:])
        write_json(path, d)
