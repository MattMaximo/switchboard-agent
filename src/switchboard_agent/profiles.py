"""Local agent profiles: one file per agent, so several agents can share one computer.

    ~/.switchboard/                 (0700; override with SWITCHBOARD_HOME)
        agents/<slug>.json          (0600) one per agent: host, id, token, keys
        agents/<slug>.seen          recent message ids (replay protection)
        agent.json                  legacy single-agent file (<= 0.3.x); migrated automatically

Pick an agent with `--as NAME` or SWITCHBOARD_AS. If exactly one profile exists it is used.
"""
import json
import os
import re
import sys


class ProfileError(Exception):
    pass


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


def save(p):
    write_json(path_for(p["slug"]), p)


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


def delete(slug):
    for ext in (".json", ".seen"):
        try:
            os.remove(os.path.join(agents_dir(), slug + ext))
        except FileNotFoundError:
            pass


def migrate_legacy():
    """Move a <=0.3.x ~/.switchboard/agent.json into agents/<slug>.json. Returns the slug or None."""
    lp = legacy_path()
    try:
        with open(lp) as f:
            c = json.load(f)
    except (FileNotFoundError, ValueError):
        return None
    if not c.get("token"):
        return None
    for p in _load_all():
        if p.get("id") == c.get("id") and p.get("host") == c.get("host"):
            os.replace(lp, lp + ".migrated")
            return p["slug"]
    slug = new_slug(c.get("name", "agent"))
    p = dict(c, slug=slug, asked_name=c.get("name"), migrated_from="agent.json")
    save(p)
    os.replace(lp, lp + ".migrated")
    sys.stderr.write("Switchboard: moved this agent's identity (\"%s\") to %s\n" % (p.get("name"), path_for(slug)))
    return slug


def _load_all():
    try:
        names = sorted(os.listdir(agents_dir()))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(agents_dir(), n)) as f:
                p = json.load(f)
        except (OSError, ValueError):
            continue
        if p.get("token"):
            p["slug"] = n[:-5]
            out.append(p)
    return out


def all_profiles():
    migrate_legacy()
    return _load_all()


def choose(as_name=None):
    """Return the selected profile dict or raise ProfileError with a short, actionable message."""
    as_name = (as_name or os.environ.get("SWITCHBOARD_AS") or "").strip()
    ps = all_profiles()
    if not ps:
        raise ProfileError("This agent isn't connected yet. Join first with your network code and a name.")
    if not as_name:
        if len(ps) == 1:
            return ps[0]
        raise ProfileError("Several agents are set up on this computer: %s. Pick one with --as NAME."
                           % ", ".join('"%s"' % p["name"] for p in ps))
    key = as_name.lower()
    for test in (lambda p: p["name"].lower() == key, lambda p: p["slug"] == key, lambda p: p.get("id") == as_name,
                 lambda p: (p.get("asked_name") or "").lower() == key, lambda p: p["slug"] == slugify(as_name)):
        hits = [p for p in ps if test(p)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ProfileError("More than one agent here matches %r: %s. Use the exact name."
                               % (as_name, ", ".join('"%s"' % p["name"] for p in hits)))
    raise ProfileError("No agent named %r on this computer. Set up here: %s."
                       % (as_name, ", ".join('"%s"' % p["name"] for p in ps)))


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
