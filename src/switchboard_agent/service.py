"""Answer on its own (opt-in): a per-user login service that runs `listen --answer-with CMD` in one folder.

    switchboard listen --as NAME --answer-with "claude -p ..." --install     (run once, in the agent's folder)
    switchboard listen --as NAME --uninstall

macOS: a LaunchAgent ~/Library/LaunchAgents/<label>.plist (RunAtLoad, restarted if it crashes).
Linux: a systemd user unit ~/.config/systemd/user/<label>.service (starts with the user's session; with
`loginctl enable-linger` also before login). Windows: not supported (run listen in a terminal instead).

It starts at login, so questions that arrived while the computer was off are answered when it next starts:
listen's first poll returns the whole waiting mailbox. The service is removed again by --uninstall, by
`leave`, and when the server says the owner removed this agent. A service whose agent is gone from this
computer removes itself on its next start instead of restarting forever.

Labels are org.pioneerdigital.switchboard.<slug>, plus a short hash of SWITCHBOARD_HOME when that isn't the
default, so a test home never shares a label with a real agent.
"""
import hashlib
import os
import plistlib
import shlex
import shutil
import subprocess
import sys

from . import profiles

PREFIX = "org.pioneerdigital.switchboard."
SERVICE_ENV = "SWITCHBOARD_SERVICE"  # set inside the service: the label it runs as


class ServiceError(Exception):
    pass


def label(slug):
    h = os.environ.get("SWITCHBOARD_HOME")
    tag = ""
    if h and os.path.abspath(os.path.expanduser(h)) != os.path.abspath(os.path.expanduser("~/.switchboard")):
        tag = "." + hashlib.sha256(os.path.abspath(os.path.expanduser(h)).encode()).hexdigest()[:8]
    return PREFIX + slug + tag


def _platform():
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def path_for(slug):
    lb = label(slug)
    if _platform() == "mac":
        return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % lb)
    if _platform() == "linux":
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "systemd", "user", lb + ".service")
    return None


def runner():
    """How the service starts this package: uvx (always the newest release) or the installed command."""
    from .client import cmd_prefix
    if cmd_prefix().startswith("uvx"):
        uvx = shutil.which("uvx")
        if uvx:
            return [os.path.abspath(uvx), "switchboard-agent@latest"]
    exe = shutil.which("switchboard-agent")
    if exe:
        return [os.path.abspath(exe)]
    return [sys.executable, "-m", "switchboard_agent"]


def answer_argv(command):
    """The --answer-with command with its program as an absolute path (a login service has a bare PATH)."""
    try:
        argv = shlex.split(command or "")
    except ValueError as e:
        raise ServiceError("Couldn't read the --answer-with command (%s)." % e)
    if not argv:
        raise ServiceError("--install needs --answer-with, e.g. --answer-with \"claude -p\".")
    exe = shutil.which(argv[0])
    if not exe:
        raise ServiceError("Can't find %r (the --answer-with command) on this computer." % argv[0])
    return [os.path.abspath(exe)] + argv[1:]


def _env(lb):
    # PATH as the user has it, minus uv's throwaway tool folders (this very uvx run puts one first)
    path = [d for d in (os.environ.get("PATH") or "").split(os.pathsep) if d and "/archive-v" not in d.replace("\\", "/")]
    env = {SERVICE_ENV: lb, "PATH": os.pathsep.join(path) or "/usr/bin:/bin:/usr/sbin:/sbin"}
    for k, v in os.environ.items():  # keep this agent's home and Claude config, and the user's uv settings
        if v and (k in ("SWITCHBOARD_HOME", "SWITCHBOARD_HOST", "CLAUDE_CONFIG_DIR", "LANG", "HOME") or k.startswith("UV_")):
            env[k] = v
    return env


def program(slug, command, timeout=None):
    args = runner() + ["listen", "--as", slug, "--answer-with", shlex.join(answer_argv(command))]
    if timeout:
        args += ["--answer-timeout", str(int(timeout))]
    return args


def log_path(slug):
    return os.path.join(profiles.home(), "logs", slug + ".listen.log")


def plist_bytes(slug, args, folder):
    lb = label(slug)
    log = log_path(slug)
    return plistlib.dumps({
        "Label": lb,
        "ProgramArguments": args,
        "WorkingDirectory": folder,
        "EnvironmentVariables": _env(lb),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a clean stop
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    })


def _sd_quote(s):
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"%s"' % s


def unit_text(slug, args, folder, name):
    lb = label(slug)
    env = "\n".join("Environment=%s" % _sd_quote("%s=%s" % kv) for kv in sorted(_env(lb).items()))
    return ("[Unit]\nDescription=Switchboard: %s answers your other agents on its own\n\n"
            "[Service]\nWorkingDirectory=%s\nExecStart=%s\n%s\nRestart=on-failure\nRestartSec=30\n"
            "StandardOutput=append:%s\nStandardError=append:%s\n\n[Install]\nWantedBy=default.target\n"
            % (name.replace("\n", " "), _sd_quote(folder), " ".join(_sd_quote(a) for a in args), env,
               log_path(slug), log_path(slug)))


def _run(argv, detach=False):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30, start_new_session=detach)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 1, "", str(e))


def installed(slug):
    p = path_for(slug)
    return bool(p and os.path.exists(p))


def running(slug):
    """True when the service manager has it loaded (macOS) or active (Linux)."""
    lb = label(slug)
    if _platform() == "mac":
        return _run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), lb)]).returncode == 0
    if _platform() == "linux":
        return _run(["systemctl", "--user", "is-active", "--quiet", lb + ".service"]).returncode == 0
    return False


def install(slug, name, command, folder=None, timeout=None):
    """Install (or replace) the login service for this agent. Returns the file path."""
    plat = _platform()
    if not plat:
        raise ServiceError("Answer on its own needs macOS or Linux. On this computer, run the listen command in a "
                           "terminal and leave it open.")
    folder = os.path.abspath(folder or os.getcwd())
    args = program(slug, command, timeout)
    os.makedirs(os.path.dirname(log_path(slug)), exist_ok=True)
    path = path_for(slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if installed(slug):
        remove(slug)
    lb = label(slug)
    if plat == "mac":
        with open(path, "wb") as f:
            f.write(plist_bytes(slug, args, folder))
        r = _run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), path])
        if r.returncode != 0 and not running(slug):
            r = _run(["launchctl", "load", "-w", path])  # older macOS, or a non-GUI session
        if not running(slug):
            os.remove(path)
            raise ServiceError("macOS didn't start the login service (%s). Nothing was left installed."
                               % ((r.stderr or r.stdout or "launchctl failed").strip()[:200]))
    else:
        with open(path, "w") as f:
            f.write(unit_text(slug, args, folder, name))
        _run(["systemctl", "--user", "daemon-reload"])
        r = _run(["systemctl", "--user", "enable", "--now", lb + ".service"])
        if r.returncode != 0:
            os.remove(path)
            _run(["systemctl", "--user", "daemon-reload"])
            raise ServiceError("systemd didn't start the user service (%s). Nothing was left installed."
                               % ((r.stderr or r.stdout or "systemctl failed").strip()[:200]))
    return path


def remove(slug):
    """Stop and delete this agent's login service. Returns True if there was one. Never raises.
    Safe to call from inside the service itself: the file goes first, then the stop request is sent
    from a detached process (the stop ends this process too)."""
    try:
        plat, path, lb = _platform(), path_for(slug), label(slug)
        if not plat:
            return False
        had = bool(path and os.path.exists(path))
        if plat == "mac":
            loaded = running(slug)
            if had:
                os.remove(path)
            if loaded:
                r = _run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), lb)], detach=True)
                if r.returncode != 0 and running(slug):
                    _run(["launchctl", "remove", lb], detach=True)
            return had or loaded
        if had:
            _run(["systemctl", "--user", "disable", lb + ".service"])
            os.remove(path)
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "stop", lb + ".service"], detach=True)
        return had
    except Exception:
        return False


def inside():
    """The label of the service this process runs in, or None."""
    return os.environ.get(SERVICE_ENV) or None
