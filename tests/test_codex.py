"""0.11.0: Codex and ChatGPT onboarding (kind rules, NEXT block, Codex skill file, safe uninstall, sandbox lines)."""
import io
import os
import stat
import unittest
from unittest import mock

from switchboard_agent import claude_skill, cli, client, profiles

from test_profiles import FakeServer, Home, peer

LINK = "https://x/j/abcdefgh.txt"


def uninstall(prefix, who):
    return ("If your user asks to remove, disconnect or uninstall Switchboard for you: run `%s leave --as '%s'`. "
            "It takes you off their dashboard and deletes only your keys. Never delete ~/.switchboard, uv or its "
            "caches, or other skill files: your user's other agents use them." % (prefix, who))


class Kind(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {})
        self.env.start()
        for k in ("CODEX_THREAD_ID", "CLAUDECODE"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()

    def test_generic_kind_inside_codex_is_codex(self):
        os.environ["CODEX_THREAD_ID"] = "019a-thread"
        for kind in ("Agent", "AI assistant", "AI coding agent", "coding agent", "WHAT YOU ARE", "what you are", "",
                     None, "an AI coding assistant", "GPT-6.2", "OpenAI model"):
            self.assertEqual(client.join_kind(kind, "Codex"), "Codex", kind)
            self.assertEqual(client.join_kind(kind, "Helper"), "Codex", kind)
        self.assertEqual(client.join_kind("Grok", "Grok"), "Grok")  # an explicit other kind wins
        self.assertEqual(client.join_kind("Hermes Agent", "Boo"), "Hermes")
        self.assertEqual(client.join_kind("Custom Thing", "X"), "Custom Thing")

    def test_outside_codex_generic_stays(self):
        self.assertEqual(client.join_kind("AI coding agent", "Codex"), "AI coding agent")
        self.assertEqual(client.join_kind("Agent", "Helper"), "Agent")

    def test_chatgpt_name_or_kind_wins(self):
        for env in ({}, {"CODEX_THREAD_ID": "t"}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(client.join_kind("AI assistant", "ChatGPT"), "ChatGPT")
                self.assertEqual(client.join_kind("Agent", "ChatGPT Work"), "ChatGPT")
                self.assertEqual(client.join_kind("Codex", "ChatGPT"), "ChatGPT")
                self.assertEqual(client.join_kind("ChatGPT desktop (Work, Local)", "Assistant"), "ChatGPT")
                self.assertEqual(client.join_kind("chat-gpt", None), "ChatGPT")
                self.assertEqual(client.join_kind("Claude Code", "ChatGPT fan"), "Claude Code")  # explicit non-OpenAI kind

    def test_claude_code_rule_unchanged(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertEqual(client.join_kind("Claude", "Claude"), "Claude Code")
            self.assertEqual(client.join_kind("Agent", "X"), "Claude Code")


class CodexJoin(Home):
    def setUp(self):
        super().setUp()
        self.cwd = os.getcwd()
        self.work = os.path.realpath(os.path.join(self.d, "work"))
        os.makedirs(self.work)
        os.chdir(self.work)
        self.cx_skills = os.path.join(self.d, "scratch-codex-skills")
        os.environ["SWITCHBOARD_CODEX_SKILLS"] = self.cx_skills
        self.skill = os.path.join(self.cx_skills, "switchboard", "SKILL.md")
        self.cc_skill = os.path.join(self.d, "claude", "skills", "switchboard", "SKILL.md")
        os.environ["CODEX_THREAD_ID"] = "019a-thread"

    def tearDown(self):
        os.chdir(self.cwd)
        super().tearDown()

    def cli(self, srv, *argv):
        buf = io.StringIO()
        with mock.patch.object(client, "api", srv), mock.patch("sys.stdout", buf):
            cli.main(list(argv))
        return buf.getvalue()

    def join(self, srv, name="Codex", kind="AI coding agent"):
        return self.cli(srv, "join", LINK, "--name", name, "--kind", kind)

    def test_codex_next_block_exact_no_hello(self):
        srv = FakeServer(["Codex"], ["a2"], peers=[peer("Boo", "b1")])
        out = self.join(srv)
        self.assertEqual(srv.sent, [])
        self.assertEqual(srv.joined[0]["kind"], "Codex")
        p = client.cmd_prefix()
        self.assertEqual(out, "\n".join([
            'Connected to Matt\'s Switchboard as "Codex".',
            "Saved for later sessions: your switchboard skill has the commands (--as 'Codex').",
            uninstall(p, "Codex"),
            "",
            "NEXT (Codex): setup is done. Don't message anyone or run anything else. Your whole reply to your user is "
            "exactly this one line, with nothing before or after it, then stop:",
            "Connected to your Switchboard as Codex.",
        ]) + "\n")

    def test_chatgpt_next_block_and_skill(self):
        srv = FakeServer(["ChatGPT"], ["a2"])
        out = self.join(srv, "ChatGPT", "AI assistant")
        self.assertEqual(srv.joined[0]["kind"], "ChatGPT")
        self.assertIn("NEXT (ChatGPT): setup is done. Don't message anyone or run anything else. Your whole reply", out)
        self.assertTrue(out.endswith(":\nConnected to your Switchboard as ChatGPT.\n"), out)
        txt = open(self.skill).read()
        self.assertIn('as "ChatGPT"', txt)
        self.assertIn("--as 'ChatGPT'", txt)
        self.assertNotIn("Claude", txt)
        self.assertFalse(os.path.exists(self.cc_skill))

    def test_codex_skill_written_with_commands_and_uninstall(self):
        self.join(FakeServer(["Codex"], ["a2"]))
        txt = open(self.skill).read()
        self.assertTrue(txt.startswith("---\nname: switchboard\ndescription: " + claude_skill.DESCRIPTION + "\n---\n"))
        self.assertIn(claude_skill.MARK, txt)
        self.assertIn("%s ask NAME \"question\" --as 'Codex'" % client.cmd_prefix(), txt)
        self.assertIn(uninstall(client.cmd_prefix(), "Codex"), txt)
        self.assertNotIn("Claude", txt)
        c = profiles.choose()
        self.assertEqual(c["codex_skill"], self.skill)
        self.assertNotIn("claude_skill", c)
        self.assertEqual(c["folder"], self.work)
        self.assertFalse(os.path.exists(self.cc_skill))

    def test_codex_home_default_path(self):
        os.environ.pop("SWITCHBOARD_CODEX_SKILLS")
        with mock.patch.dict(os.environ, {"CODEX_HOME": os.path.join(self.d, "cxhome")}):
            self.assertEqual(claude_skill.skill_path("codex"), os.path.join(self.d, "cxhome", "skills", "switchboard", "SKILL.md"))
        with mock.patch.dict(os.environ, {"HOME": self.d}):
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(claude_skill.skill_path("codex"), os.path.join(self.d, ".codex", "skills", "switchboard", "SKILL.md"))

    def test_users_own_codex_skill_is_never_touched(self):
        os.makedirs(os.path.dirname(self.skill))
        mine = "---\nname: switchboard\ndescription: mine\n---\nmy notes\n"
        open(self.skill, "w").write(mine)
        out = self.join(FakeServer(["Codex"], ["a2"]))
        self.assertIn("For later: %s ask|send|inbox --as 'Codex'" % client.cmd_prefix(), out)
        self.assertIn(uninstall(client.cmd_prefix(), "Codex"), out)
        self.assertEqual(open(self.skill).read(), mine)
        self.assertNotIn("codex_skill", profiles.choose())
        out = self.cli(FakeServer([], []), "leave", "--as", "Codex")
        self.assertEqual(open(self.skill).read(), mine)

    def test_chatgpt_and_codex_merge_then_leave(self):
        self.join(FakeServer(["Codex"], ["a2"]))
        other = os.path.realpath(os.path.join(self.d, "chatgpt-folder"))
        os.makedirs(other)
        os.chdir(other)
        self.join(FakeServer(["ChatGPT"], ["a3"]), "ChatGPT", "AI assistant")
        txt = open(self.skill).read()
        self.assertRegex(txt, r"Several (Codex and ChatGPT|ChatGPT and Codex) agents on this computer")  # joined the same second
        self.assertIn("- %s (Codex): --as 'Codex'" % self.work, txt)
        self.assertIn("- %s (ChatGPT): --as 'ChatGPT'" % other, txt)
        self.assertIn(uninstall(client.cmd_prefix(), "YOUR NAME"), txt)
        self.cli(FakeServer([], []), "leave", "--as", "Codex")
        txt = open(self.skill).read()
        self.assertNotIn("Several", txt)
        self.assertIn("--as 'ChatGPT'", txt)
        self.cli(FakeServer([], []), "leave", "--as", "ChatGPT")
        self.assertFalse(os.path.exists(os.path.dirname(self.skill)))

    def test_leave_removes_only_its_own_profile_and_skill(self):
        os.environ.pop("CODEX_THREAD_ID")
        self.join(FakeServer(["Claude Code"], ["c1"]), "Claude Code", "Claude Code")
        os.environ["CODEX_THREAD_ID"] = "t"
        self.join(FakeServer(["Codex"], ["a2"]))
        cc_before = open(self.cc_skill).read()
        sibling = os.path.join(self.cx_skills, "mine", "SKILL.md")
        os.makedirs(os.path.dirname(sibling))
        open(sibling, "w").write("x")
        srv = FakeServer([], [])
        calls = []

        def spy(host, method, path, body=None, token=None, timeout=40, **kw):
            calls.append((path, token))
            return srv(host, method, path, body, token, timeout)
        out = self.cli(spy, "leave", "--as", "Codex")
        self.assertEqual(out, '"Codex" left your Switchboard and its keys were deleted. Your other agents on this '
                              "computer are unchanged.\n")
        self.assertIn(("/api/agent/leave", "u1.a2.s"), calls)
        self.assertEqual([p["name"] for p in profiles.all_profiles()], ["Claude Code"])
        self.assertFalse(os.path.exists(self.skill))
        self.assertEqual(open(self.cc_skill).read(), cc_before)  # the other app's skill is untouched
        self.assertEqual(open(sibling).read(), "x")
        self.assertTrue(os.path.isdir(self.d))

    def test_removed_by_owner_removes_codex_skill(self):
        self.join(FakeServer(["Codex"], ["a2"]))

        def gone(*a, **k):
            raise client.SwitchboardError("removed", "removed_by_owner", {"owner": "Matt"})
        with mock.patch.object(client, "api", gone):
            with self.assertRaises(client.SwitchboardError):
                client.Agent("Codex").inbox()
        self.assertFalse(os.path.exists(self.skill))

    def test_prune_dead_removes_codex_skill(self):
        self.join(FakeServer(["Codex"], ["a2"]))
        self.join(FakeServer(["Codex 2"], ["a3"], dead={"a2"}), "Codex", "Codex")  # join prunes a2 first
        txt = open(self.skill).read()
        self.assertNotIn("Several", txt)
        self.assertIn("--as 'Codex 2'", txt)


class Sandbox(Home):
    def setUp(self):
        super().setUp()
        self.ro = os.path.join(self.d, "ro")
        os.makedirs(self.ro)
        os.chmod(self.ro, stat.S_IRUSR | stat.S_IXUSR)
        os.environ["SWITCHBOARD_HOME"] = os.path.join(self.ro, ".switchboard")
        self.calls = []

    def tearDown(self):
        os.chmod(self.ro, 0o700)
        super().tearDown()

    def spy(self, *a, **k):
        self.calls.append(a[:3])
        raise AssertionError("no server call allowed")

    def run_join(self, env):
        with mock.patch.dict(os.environ, env), mock.patch.object(client, "api", self.spy):
            with self.assertRaises(client.SwitchboardError) as cm:
                client.join(LINK, "Codex", "Codex")
        self.assertEqual(self.calls, [])
        return str(cm.exception)

    def test_read_only_home_no_server_call(self):
        msg = self.run_join({})
        self.assertIn("so nothing was joined and your invite is still good", msg)
        self.assertIn("Don't change SWITCHBOARD_HOME.", msg)

    def test_read_only_home_inside_codex_sandbox(self):
        msg = self.run_join({"CODEX_SANDBOX": "seatbelt"})
        self.assertEqual(msg, client.CODEX_NO_WRITE % os.path.join(self.ro, ".switchboard"))
        self.assertIn("Nothing was used up. Run this same join command again with approval to go outside the sandbox.", msg)

    def test_no_network_sandbox_line(self):
        msg = self.run_join({"CODEX_SANDBOX": "seatbelt", "CODEX_SANDBOX_NETWORK_DISABLED": "1"})
        self.assertEqual(msg, "You're inside Codex's sandbox without network. Nothing was used up. Run this same join "
                              "command again with approval to go outside the sandbox.")

    def test_unreachable_inside_codex_no_network(self):
        os.chmod(self.ro, 0o700)

        def down(*a, **k):
            raise client.SwitchboardError("down", "unreachable")
        for env, want in (({"CODEX_SANDBOX_NETWORK_DISABLED": "1"}, client.CODEX_NO_NET), ({}, "switch to Work")):
            with mock.patch.dict(os.environ, env), mock.patch.object(client, "api", down):
                with self.assertRaises(client.SwitchboardError) as cm:
                    client.join(LINK, "Codex", "Codex")
            self.assertIn(want, str(cm.exception))
        self.assertIn("In the ChatGPT desktop app on your computer, switch to Work, set it to Local and pick a folder, "
                      "then paste your message again.", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
