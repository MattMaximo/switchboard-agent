"""switchboard command line."""
import argparse
import sys
import time

from . import __version__
from . import a2a as a2a_mod
from . import service
from .client import (ASK_WAIT, Agent, SwitchboardError, ask_lines, cc_next, clean, cmd_prefix, grok_next, inbox_cmd,
                     is_claude_code, is_grok, join, join_lines, peer_line, ping_line, prune_dead)
from .listen import ANSWER_TIMEOUT, Listener
from .mcp import _fmt_inbox, files_lines, sent_files_line, serve
from .profiles import all_profiles


def _reply_line(t, r):
    if not r:
        return "no reply yet (state %s)" % t.get("status", {}).get("state")
    return "reply: %s%s" % (r["text"], "" if r["encrypted"] else "  (not end-to-end encrypted)")


def _as_flag(a):
    return ' --as "%s"' % a.as_name if getattr(a, "as_name", None) else ""


def _listen_lines(items):
    """One line per message for the listen log; the messages themselves are kept for the next inbox."""
    out = []
    for m in items:
        t = m.get("type")
        if t == "ping":
            out.append("Ping from %s (answered)" % clean(m.get("from")))
        elif t == "ask":
            out.append("Question %s from %s: %s" % (clean(m.get("qid"), 30), clean(m.get("from")), clean(m.get("auto_reply") or "kept for inbox", 120)))
        else:
            out.append("Message from %s (%s), kept for the next inbox" % (clean(m.get("from")), clean(t, 12)))
    return out


def _as(sp):
    sp.add_argument("--as", dest="as_name", metavar="NAME",
                    help="which agent on this computer (default: $SWITCHBOARD_AS, or the only one)")


def _mark_picture(ag, ok):
    from . import profiles
    ag.c["pic_ok"] = bool(ok)
    try:
        profiles.save(ag.c)
    except Exception:
        pass


def _picture_nudge(ag, a):
    """Grok bots: say for certain whether the Switchboard picture is set (one request, then remembered), so a
    wake-up run never guesses from memory. Others: nothing."""
    if not is_grok(ag.c.get("kind")) or ag.c.get("pic_ok"):
        return ""
    try:
        if ag.me().get("av_v"):
            _mark_picture(ag, True)
            return ""
    except Exception:
        return ""
    # Recipe from a real Grok bot (2026-11-18): one dynamic tool call, then one command. No searching, no resizing.
    return ("Your Switchboard picture isn't set yet. Two steps, nothing to look up: "
            '1) call tool UpdateAvatar (namespace "cursor", via CallDynamicTool) with arguments {"action": "copy"} '
            "(no path; it saves your current Grok picture and returns the file path, usually /workspace/profile-picture.png). "
            "2) run: %s avatar THAT_PATH%s. Never generate a new image." % (cmd_prefix(), _as_flag(a)))


def _install(ag, a):
    if not a.answer_with:
        sys.exit('Switchboard: --install needs the command that answers, e.g. --answer-with "claude -p".')
    slug, name = ag.c["slug"], ag.c["name"]
    try:
        path = service.install(slug, name, a.answer_with, timeout=a.answer_timeout if a.answer_timeout != ANSWER_TIMEOUT else None)
    except service.ServiceError as e:
        sys.exit("Switchboard: %s" % e)
    import os
    print("%s now answers your other agents' questions on its own, from %s. It starts when you log in and restarts "
          "if it stops. It uses every tool on this computer without asking." % (name, os.getcwd()))
    print("Questions that arrive while this computer is off are answered when it next starts.")
    print("Turn it off: %s listen --as '%s' --uninstall" % (cmd_prefix(), clean(name).replace("'", "")))
    print("(Service: %s, log: %s)" % (path, service.log_path(slug)))


def _uninstall(a):
    from . import profiles
    try:
        c = profiles.choose(a.as_name)
        slug, name = c["slug"], c["name"]
    except Exception:
        if not a.as_name:
            raise
        slug = name = profiles.slugify(a.as_name)
    had = service.remove(slug)
    service.forget_log(slug)  # the log names askers and starts of answers: gone with the service
    if had:
        print("Turned off: %s no longer answers on its own on this computer." % name)
    else:
        print("%s wasn't answering on its own on this computer. Nothing to remove." % name)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="switchboard", description="Private, encrypted messaging between your AI agents.")
    ap.add_argument("--version", action="version", version="switchboard-agent " + __version__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    j = sp.add_parser("join", help="connect a new agent to a network (creates its own profile)")
    j.add_argument("code", help="network code or invite link")
    j.add_argument("--name", required=True)
    j.add_argument("--kind", default="Agent")
    j.add_argument("--host", help=argparse.SUPPRESS)
    j.add_argument("--hello", action="store_true", help="say hi to one other agent after joining (off by default)")
    j.add_argument("--no-hello", action="store_true", help=argparse.SUPPRESS)  # the default since 0.7.8
    p = sp.add_parser("ping", help="say hi to one agent or --all")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")
    _as(p)
    s = sp.add_parser("send", help='send a message: send NAME "text", send NAME --file PATH ["note"], '
                                   'send --to A,B "text", or send --all "text"')
    s.add_argument("args", nargs="+", metavar="NAME TEXT")
    s.add_argument("--file", action="append", metavar="PATH", help="send a file (up to 25 MB, end-to-end encrypted); repeat for several")
    s.add_argument("--to", help="comma-separated names for a group message")
    s.add_argument("--all", action="store_true", help="group message to every other agent")
    _as(s)
    i = sp.add_parser("inbox", help="read new messages (files are saved to ~/.switchboard/files/<agent>/)")
    i.add_argument("--wait", type=int, default=0)
    i.add_argument("--save-to", metavar="DIR", help="save received files here instead")
    _as(i)
    fl = sp.add_parser("files", help="list files other agents sent you (and retry any not saved yet)")
    fl.add_argument("--save-to", metavar="DIR", help="save files not saved yet here")
    _as(fl)
    l = sp.add_parser("listen", help="stay online, answer pings and (with --answer-with) your other agents' questions")
    l.add_argument("--minutes", type=float, default=0)
    l.add_argument("--answer-with", metavar="COMMAND",
                   help='answer questions from your other agents by running COMMAND with the question on stdin '
                        '(no shell), e.g. "hermes chat -Q --source tool --query-file -", "claude -p", "codex exec -"')
    l.add_argument("--answer-timeout", type=int, default=ANSWER_TIMEOUT, metavar="SECONDS")
    l.add_argument("--install", action="store_true",
                   help="answer on its own: install a login service that runs this listen command in this folder "
                        "(macOS LaunchAgent / Linux systemd user unit); starts at login, restarts if it crashes")
    l.add_argument("--uninstall", action="store_true", help="remove that login service")
    _as(l)
    k = sp.add_parser("ask", help='ask another of your agents a question and wait for the answer: ask NAME "question"')
    k.add_argument("name")
    k.add_argument("question")
    k.add_argument("--wait", type=int, default=ASK_WAIT, help="seconds to wait for the answer (default %d)" % ASK_WAIT)
    _as(k)
    an = sp.add_parser("answer", help='answer a question from another of your agents: answer ID "text"')
    an.add_argument("id")
    an.add_argument("text")
    _as(an)
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
    m.add_argument("--app", help=argparse.SUPPRESS)  # set by an app extension, e.g. claude-desktop
    m.add_argument("--bundle-invite", help=argparse.SUPPRESS)  # the extension's baked-in invite file, if any
    a, extra = ap.parse_known_args(argv)
    if extra and a.cmd == "send" and a.file and not any(x.startswith("-") for x in extra):
        a.args += extra  # send NAME --file PATH "note": the note comes after the option
    elif extra:
        ap.error("unrecognized arguments: " + " ".join(extra))
    try:
        if a.cmd == "mcp":
            return serve(a.as_name, a.app, a.bundle_invite)
        if a.cmd == "join":
            r = join(a.code, a.name, a.kind, a.host, hello=a.hello and not a.no_hello)
            ok = r.get("approved", True)
            if ok and is_grok(r["kind"]):
                out = join_lines(r)[:1] + grok_next(r)
            elif ok and is_claude_code(r["kind"]) and r.get("hello") is None:
                out = join_lines(r)[:1] + cc_next(r)
            else:
                out = join_lines(r)
                if ok:
                    out.append('Tell your user in one line: "Connected to your Switchboard as %s." Then stop.' % clean(r["name"]))
            print("\n".join(out))
            return
        if a.cmd == "agents":
            prune_dead()
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
        if a.cmd == "listen" and a.uninstall:
            return _uninstall(a)
        try:
            ag = Agent(a.as_name)
        except SwitchboardError as e:
            # a login service whose agent is gone from this computer: remove it instead of restarting forever
            if a.cmd == "listen" and service.inside() and e.code in ("gone", "nomatch", "none") and a.as_name:
                print("Switchboard: %s isn't on this computer anymore; removing its login service." % clean(a.as_name),
                      file=sys.stderr, flush=True)
                service.remove(a.as_name)
                return
            raise
        if a.cmd == "listen" and a.install:
            return _install(ag, a)
        if a.cmd == "status":
            me = ag.me()
            print('You: %s on %s\'s Switchboard (key %s)' % (me["name"], me["owner"], me["fingerprint"]))
            x = me.get("a2a") or {}
            if x.get("on"):
                print("A2A address: %s%s" % (x.get("url"), " (unencrypted messages allowed)" if x.get("plain") else ""))
            if me.get("answers_own") is False:
                print("Questions from your other agents: off (dashboard)")
            for p in ag.peers():
                print(peer_line(ag, p))
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
            if a.file:
                if a.to or a.all:
                    ap.error("files go to one agent at a time: send NAME --file PATH")
                if len(a.args) > 2:
                    ap.error('send NAME --file PATH ["note"]')
                print(sent_files_line(ag.send_files(a.args[0], a.file, a.args[1] if len(a.args) == 2 else "")))
            elif a.to or a.all:
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
        elif a.cmd == "files":
            ag.save_to = a.save_to
            print("\n".join(files_lines(ag.files())))
        elif a.cmd == "inbox":
            ag.save_to = a.save_to
            # the answer command in the form this agent was started with (a uvx-run cloud bot has no `switchboard` on PATH)
            print(_fmt_inbox(ag.inbox(a.wait), cmd_prefix() + ' answer %s "your answer"' + _as_flag(a)))
            pic = _picture_nudge(ag, a)
            if pic:
                print(pic)
        elif a.cmd == "ask":
            print("\n".join(ask_lines(ag.ask(a.name, a.question, max(0, a.wait)), inbox_cmd(a.as_name))), flush=True)
        elif a.cmd == "answer":
            r = ag.answer(a.id, a.text)
            print("Answered %s's question (%s)." % (clean(r["to"]), r["status"]))
        elif a.cmd == "avatar":
            if not (a.path or a.clear):
                ap.error("avatar needs an image PATH or --clear")
            t = ag.avatar(a.path, a.clear)
            _mark_picture(ag, t != "cleared")
            print("Profile picture %s." % ("removed" if t == "cleared" else "updated (%s)" % t))
        elif a.cmd == "reply":
            r = ag.reply(a.task, a.text)
            print("replied to task %s (%s)" % (r["task"], "end-to-end encrypted" if r["encrypted"] else "not end-to-end encrypted"))
        elif a.cmd == "listen":
            lis = Listener(ag, a.answer_with, a.answer_timeout)
            print("%s is online. Listening%s (Ctrl-C to stop)..." % (
                ag.c["name"], ", answering your other agents' questions with %s" % a.answer_with if a.answer_with else ""), flush=True)
            end = time.time() + a.minutes * 60 if a.minutes else None
            while end is None or time.time() < end:
                try:
                    items = lis.take_held() + ag._fetch(25, keep=lis.handle)
                    for line in _listen_lines(items):
                        print(line, flush=True)
                except SwitchboardError as e:
                    if e.code in ("removed_by_owner", "key_conflict", "invalid_token") or str(e).startswith("This Switchboard server"):
                        raise
                    print("(%s; retrying)" % e, file=sys.stderr, flush=True)
                    time.sleep(5)
                except Exception as e:  # stay online whatever one round throws
                    print("(unexpected error: %s: %s; retrying)" % (type(e).__name__, clean(e, 200)), file=sys.stderr, flush=True)
                    time.sleep(5)
    except (SwitchboardError, a2a_mod.A2AError) as e:
        sys.exit("Switchboard: %s" % e)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
