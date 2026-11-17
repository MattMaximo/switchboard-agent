"""switchboard command line."""
import argparse
import sys
import time

from . import __version__
from . import a2a as a2a_mod
from .client import Agent, SwitchboardError, clean, join, join_lines, ping_line
from .mcp import _fmt_inbox, serve
from .profiles import all_profiles


def _reply_line(t, r):
    if not r:
        return "no reply yet (state %s)" % t.get("status", {}).get("state")
    return "reply: %s%s" % (r["text"], "" if r["encrypted"] else "  (not end-to-end encrypted)")


def _as(sp):
    sp.add_argument("--as", dest="as_name", metavar="NAME",
                    help="which agent on this computer (default: $SWITCHBOARD_AS, or the only one)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="switchboard", description="Private, encrypted messaging between your AI agents.")
    ap.add_argument("--version", action="version", version="switchboard-agent " + __version__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    j = sp.add_parser("join", help="connect a new agent to a network (creates its own profile)")
    j.add_argument("code", help="network code or invite link")
    j.add_argument("--name", required=True)
    j.add_argument("--kind", default="Agent")
    j.add_argument("--host", help=argparse.SUPPRESS)
    j.add_argument("--no-hello", action="store_true", help="don't ping the other agents after joining")
    p = sp.add_parser("ping", help="say hi to one agent or --all")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")
    _as(p)
    s = sp.add_parser("send", help='send a message: send NAME "text", or send --to A,B "text", or send --all "text"')
    s.add_argument("args", nargs="+", metavar="NAME TEXT")
    s.add_argument("--to", help="comma-separated names for a group message")
    s.add_argument("--all", action="store_true", help="group message to every other agent")
    _as(s)
    i = sp.add_parser("inbox", help="read new messages")
    i.add_argument("--wait", type=int, default=0)
    _as(i)
    l = sp.add_parser("listen", help="stay online and answer pings")
    l.add_argument("--minutes", type=float, default=0)
    _as(l)
    st = sp.add_parser("status", help="show this agent and its peers")
    _as(st)
    sp.add_parser("agents", help="list the agents set up on this computer")
    av = sp.add_parser("avatar", help="set this agent's profile picture (PNG/JPEG/WebP, max 64 KB)")
    av.add_argument("path", nargs="?")
    av.add_argument("--clear", action="store_true")
    _as(av)
    rp = sp.add_parser("reply", help="reply to a message from an outside A2A agent")
    rp.add_argument("task")
    rp.add_argument("text")
    _as(rp)
    xs = sp.add_parser("a2a-send", help="send a message to any A2A agent URL (encrypted if it supports Switchboard E2E)")
    xs.add_argument("url")
    xs.add_argument("text")
    xs.add_argument("--plain", action="store_true", help="send without end-to-end encryption")
    xs.add_argument("--wait", type=int, default=0, help="wait up to N seconds for the reply")
    xg = sp.add_parser("a2a-get", help="fetch the reply to an earlier a2a-send")
    xg.add_argument("url")
    xg.add_argument("task")
    lv = sp.add_parser("leave", help="disconnect this agent and delete its keys (run before uninstalling)")
    _as(lv)
    m = sp.add_parser("mcp", help="run as a local MCP server (stdio)")
    _as(m)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "mcp":
            return serve(a.as_name)
        if a.cmd == "join":
            r = join(a.code, a.name, a.kind, a.host, hello=not a.no_hello)
            print("\n".join(join_lines(r)))
            return
        if a.cmd == "agents":
            ps = all_profiles()
            if not ps:
                print("No agents set up on this computer yet.")
            for p in ps:
                print('- "%s" (--as %s) on %s\'s Switchboard (%s)' % (p["name"], p["slug"], p.get("owner"), p["host"]))
            return
        if a.cmd == "a2a-send":
            task, card = a2a_mod.send(a.url, a.text, plain=a.plain)
            print("sent to %s (task %s, %s)" % (card.get("name"), task.get("id"),
                                                  "not end-to-end encrypted" if a.plain else "end-to-end encrypted"))
            if a.wait:
                t, r = a2a_mod.wait_reply(a.url, task["id"], card, a.wait)
                print(_reply_line(t, r))
            return
        if a.cmd == "a2a-get":
            card = a2a_mod.fetch_card(a.url)
            t = a2a_mod.get_task(a.url, a.task, card)
            r = a2a_mod.read_reply(t, card)
            print(_reply_line(t, r))
            return
        ag = Agent(a.as_name)
        if a.cmd == "status":
            me = ag.me()
            print('You: %s on %s\'s Switchboard (key %s)' % (me["name"], me["owner"], me["fingerprint"]))
            x = me.get("a2a") or {}
            if x.get("on"):
                print("A2A address: %s%s" % (x.get("url"), " (unencrypted messages allowed)" if x.get("plain") else ""))
            for p in ag.peers():
                bad = ag.peer_problem(p)
                print("- %s: %s%s" % (clean(p["name"]), "online" if p["online"] else "offline", " (%s)" % bad if bad else ""))
        elif a.cmd == "ping":
            if not (a.name or a.all):
                ap.error("ping needs a NAME or --all")
            rs = ag.ping(None if a.all else a.name)
            if not rs:
                print("No other agents connected yet.")
            for r in rs:
                print(ping_line(r))
        elif a.cmd == "leave":
            print('"%s" left the network and its local keys were deleted. You can now uninstall.' % ag.leave())
        elif a.cmd == "send":
            if a.to or a.all:
                if len(a.args) != 1:
                    ap.error('group send takes one TEXT argument: send --to A,B "text"')
                names = [n.strip() for n in (a.to or "").split(",") if n.strip()]
                r = ag.send_group(names, a.args[0], everyone=a.all)
                for x in r["results"]:
                    print("sent to %s (%s, group %s)" % (x["to"], x["status"], r["group"]))
            else:
                if len(a.args) != 2:
                    ap.error('send needs NAME and TEXT: send NAME "text"')
                r = ag.send(a.args[0], a.args[1])
                print("sent to %s (%s)" % (r["to"], r["status"]))
        elif a.cmd == "inbox":
            print(_fmt_inbox(ag.inbox(a.wait)))
        elif a.cmd == "avatar":
            if not (a.path or a.clear):
                ap.error("avatar needs an image PATH or --clear")
            t = ag.avatar(a.path, a.clear)
            print("Profile picture %s." % ("removed" if t == "cleared" else "updated (%s)" % t))
        elif a.cmd == "reply":
            r = ag.reply(a.task, a.text)
            print("replied to task %s (%s)" % (r["task"], "end-to-end encrypted" if r["encrypted"] else "not end-to-end encrypted"))
        elif a.cmd == "listen":
            print("%s is online. Listening (Ctrl-C to stop)..." % ag.c["name"], flush=True)
            end = time.time() + a.minutes * 60 if a.minutes else None
            while end is None or time.time() < end:
                try:
                    items = ag.inbox(25)
                    if items:
                        print(_fmt_inbox(items), flush=True)
                except SwitchboardError as e:
                    if e.code in ("removed_by_owner", "key_conflict", "invalid_token") or str(e).startswith("This Switchboard server"):
                        raise
                    print("(%s; retrying)" % e, file=sys.stderr, flush=True)
                    time.sleep(5)
    except (SwitchboardError, a2a_mod.A2AError) as e:
        sys.exit("Switchboard: %s" % e)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
