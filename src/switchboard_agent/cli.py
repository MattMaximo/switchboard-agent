"""switchboard command line."""
import argparse
import sys
import time

from . import __version__
from .client import Agent, SwitchboardError, join
from .mcp import _fmt_inbox, serve


def main(argv=None):
    ap = argparse.ArgumentParser(prog="switchboard", description="Private, encrypted messaging between your AI agents.")
    ap.add_argument("--version", action="version", version="switchboard-agent " + __version__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    j = sp.add_parser("join", help="connect this agent to a network")
    j.add_argument("code", help="network code or invite link")
    j.add_argument("--name", required=True)
    j.add_argument("--kind", default="Agent")
    j.add_argument("--host", help=argparse.SUPPRESS)
    p = sp.add_parser("ping", help="say hi to one agent or --all")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")
    s = sp.add_parser("send", help="send a message")
    s.add_argument("name")
    s.add_argument("text")
    i = sp.add_parser("inbox", help="read new messages")
    i.add_argument("--wait", type=int, default=0)
    l = sp.add_parser("listen", help="stay online and answer pings")
    l.add_argument("--minutes", type=float, default=0)
    sp.add_parser("status", help="show this agent and its peers")
    sp.add_parser("leave", help="disconnect this agent and delete its keys (run before uninstalling)")
    sp.add_parser("mcp", help="run as a local MCP server (stdio)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "mcp":
            return serve()
        if a.cmd == "join":
            r = join(a.code, a.name, a.kind, a.host)
            print('Connected to %s\'s Switchboard as "%s" (key %s).' % (r["owner"], r["name"], r["fingerprint"]))
            if not r.get("approved", True):
                print("Waiting for the owner to approve this agent on their dashboard.")
            return
        ag = Agent()
        if a.cmd == "status":
            me = ag.me()
            print('You: %s on %s\'s Switchboard (key %s)' % (me["name"], me["owner"], me["fingerprint"]))
            for p in ag.peers():
                print("- %s: %s" % (p["name"], "online" if p["online"] else "offline"))
        elif a.cmd == "ping":
            if not (a.name or a.all):
                ap.error("ping needs a NAME or --all")
            for r in ag.ping(None if a.all else a.name):
                print("ping -> %s: %s" % (r["to"], r["status"]))
        elif a.cmd == "leave":
            print('"%s" left the network and its local keys were deleted. You can now uninstall.' % ag.leave())
        elif a.cmd == "send":
            r = ag.send(a.name, a.text)
            print("sent to %s (%s)" % (r["to"], r["status"]))
        elif a.cmd == "inbox":
            print(_fmt_inbox(ag.inbox(a.wait)))
        elif a.cmd == "listen":
            print("%s is online. Listening (Ctrl-C to stop)..." % ag.c["name"], flush=True)
            end = time.time() + a.minutes * 60 if a.minutes else None
            while end is None or time.time() < end:
                try:
                    items = ag.inbox(25)
                    if items:
                        print(_fmt_inbox(items), flush=True)
                except SwitchboardError as e:
                    print("(%s; retrying)" % e, file=sys.stderr, flush=True)
                    time.sleep(5)
    except SwitchboardError as e:
        sys.exit("Switchboard: %s" % e)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
