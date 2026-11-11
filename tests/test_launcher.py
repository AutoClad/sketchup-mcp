"""Tests for the pool's bookkeeping in launcher.py: leasing an instance by
name, port or the session that last used it, and switching its source.

Nothing here starts SketchUp or talks to one: processes, starting and
switch_src are replaced. Run from the checkout whose code you mean to test
(src first on the path, since the venv's editable install is the main
checkout's):

    $env:PYTHONPATH = "$PWD\\src"; ~/source/sketchup-mcp/.venv/Scripts/python -m unittest discover -s tests
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if sys.platform != "win32":
    # launcher.py uses Windows' APIs as it loads (msvcrt, kernel32).
    raise unittest.SkipTest("the pool runs on Windows only")

from sketchup_mcp import launcher  # noqa: E402
from sketchup_mcp.launcher import PoolError  # noqa: E402


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        pool = self.dir / "pool"
        pool.mkdir()
        for name, value in [
            ("POOL_DIR", pool),
            ("POOL_FILE", pool / "pool.json"),
            ("LOCK_FILE", pool / "pool.lock"),
            ("MODELS_DIR", pool / "models"),
        ]:
            patcher = mock.patch.object(launcher, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Processes that run, by pid. SketchUps are 100+, holders 1-99.
        self.running = set()
        for name, fake in [("is_running", lambda pid, started: pid in self.running), ("switch_src", self.fake_switch)]:
            patcher = mock.patch.object(launcher, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.switches = []
        self.switch_result = None
        self.a = self.src("a")
        self.b = self.src("b")
        self.me = (1, 1.0)
        self.other = (2, 2.0)
        self.running |= {1, 2}

    def src(self, name, marker="shellify.rb"):
        folder = self.dir / name
        folder.mkdir()
        (folder / marker).write_text("# the extension's registrar")
        return launcher.normalize_src(str(folder))

    def fake_switch(self, port, src):
        self.switches.append((port, src))
        if isinstance(self.switch_result, Exception):
            raise self.switch_result
        return self.switch_result or (src, None)

    def pool(self, *instances):
        """Writes pool.json: one dict per instance, in port order, with its
        pid (running if given), src, lease and last_lease."""
        records = [launcher._blank(port) for port in launcher.PORTS]
        for r, spec in zip(records, instances, strict=False):
            if spec.get("pid"):
                r.update(
                    pid=spec["pid"],
                    started=float(spec["pid"]),
                    ready=True,
                    env=launcher._src_env(spec.get("src", self.a)),
                )
                self.running.add(spec["pid"])
            r["lease"] = spec.get("lease")
            r["last_lease"] = spec.get("last_lease")
        launcher._save(records)

    def records(self):
        return json.loads(launcher.POOL_FILE.read_text(encoding="utf-8"))["instances"]

    @staticmethod
    def held(who, label="a session", session=None):
        return {
            "holder_pid": who[0],
            "holder_started": who[1],
            "label": label,
            "session": session,
            "link": None,
            "leased_at": "2026-11-06T10:00:00+00:00",
            "last_used": "2026-11-06T10:00:00+00:00",
        }

    @staticmethod
    def last(label, session=None):
        return {
            "holder_pid": 50,
            "holder_started": 50.0,
            "label": label,
            "session": session,
            "link": None,
            "leased_at": "2026-11-06T09:00:00+00:00",
            "last_used": "2026-11-06T09:30:00+00:00",
            "released_at": "2026-11-06T09:31:00+00:00",
        }


class FindTest(PoolTest):
    def setUp(self):
        super().setUp()
        self.pool(
            {"pid": 101, "last_lease": self.last("Clad the canopy", session="s-1")},
            {"pid": 102, "lease": self.held(self.other, "Posts")},
            {"pid": 103, "last_lease": self.last("Canopy insets")},
            {},
        )
        self.all = launcher._load()

    def found(self, instance):
        return launcher.find(self.all, instance)["name"]

    def test_by_name_or_port(self):
        self.assertEqual(self.found("sketchup-3"), "sketchup-3")
        self.assertEqual(self.found("SketchUp-3"), "sketchup-3")
        self.assertEqual(self.found(9878), "sketchup-2")
        self.assertEqual(self.found("9880"), "sketchup-4")

    def test_by_the_label_of_the_last_or_current_holder(self):
        self.assertEqual(self.found("clad the canopy"), "sketchup-1")
        self.assertEqual(self.found("  Posts "), "sketchup-2")

    def test_by_session_id(self):
        self.assertEqual(self.found("s-1"), "sketchup-1")

    def test_a_whole_label_wins_over_part_of_one(self):
        # "canopy" is part of two labels, but "Canopy insets" is whole.
        self.assertEqual(self.found("Canopy insets"), "sketchup-3")
        self.assertEqual(self.found("insets"), "sketchup-3")

    def test_more_than_one_match_is_refused(self):
        with self.assertRaisesRegex(PoolError, "More than one.*sketchup-1.*sketchup-3"):
            self.found("canopy")

    def test_no_match_says_what_the_pool_has(self):
        with self.assertRaisesRegex(
            PoolError,
            'No pool instance is called "roof".*'
            'sketchup-1 \\(port 9877, free, last used by "Clad the canopy"\\).*'
            'sketchup-2 \\(port 9878, leased to "Posts"\\).*'
            "sketchup-4 \\(port 9880, not running\\)",
        ):
            self.found("roof")


class LastLeaseTest(PoolTest):
    def test_release_keeps_the_lease_as_the_last_one(self):
        self.pool({"pid": 101, "lease": self.held(self.me, "Canopy", session="s-1")})
        launcher.release(self.me)
        r = self.records()[0]
        self.assertIsNone(r["lease"])
        self.assertEqual(r["last_lease"]["label"], "Canopy")
        self.assertEqual(r["last_lease"]["session"], "s-1")
        self.assertIn("released_at", r["last_lease"])

    def test_a_holder_that_has_gone_leaves_its_lease_as_the_last_one(self):
        self.pool({"pid": 101, "lease": self.held(self.other, "Canopy")})
        self.running.discard(self.other[0])
        launcher.status()
        self.assertEqual(self.records()[0]["last_lease"]["label"], "Canopy")

    def test_a_state_file_from_before_has_no_last_lease(self):
        self.pool({"pid": 101})
        state = json.loads(launcher.POOL_FILE.read_text(encoding="utf-8"))
        for r in state["instances"]:
            del r["last_lease"]
        launcher.POOL_FILE.write_text(json.dumps(state), encoding="utf-8")
        self.assertIsNone(launcher._load()[0]["last_lease"])

    def test_a_restart_forgets_the_last_lease(self):
        # Its model goes, and with it what the last holder left.
        self.pool(
            {"pid": 101, "src": self.b, "last_lease": self.last("Canopy")},
            {"pid": 102, "lease": self.held(self.other)},
            {"pid": 103, "lease": self.held((3, 3.0))},
            {"pid": 104, "lease": self.held((4, 4.0))},
        )
        self.running |= {3, 4}
        with (
            mock.patch.object(launcher, "_kill"),
            mock.patch.object(launcher, "_launch", return_value=(105, 105.0)),
            mock.patch.object(launcher, "_wait_ready"),
        ):
            self.running.add(105)
            record = launcher.lease(self.a, self.me, label="New")
        self.assertTrue(record["fresh"])
        r = self.records()[0]
        self.assertEqual(r["lease"]["label"], "New")
        self.assertIsNone(r["last_lease"])


class NamedLeaseTest(PoolTest):
    def test_a_free_instance_on_the_same_source_is_leased_as_it_is(self):
        self.pool({"pid": 101}, {"pid": 102, "last_lease": self.last("Canopy")})
        record = launcher.lease(self.a, self.me, label="Go on", instance="Canopy")
        self.assertEqual(record["name"], "sketchup-2")
        self.assertFalse(record["fresh"])
        self.assertIn('as "Canopy" left it', record["message"])
        self.assertEqual(self.switches, [])
        r = self.records()[1]
        self.assertEqual(r["lease"]["label"], "Go on")
        self.assertEqual(r["pid"], 102)

    def test_another_source_is_switched_to_without_a_restart(self):
        self.pool({"pid": 101, "src": self.b})
        record = launcher.lease(self.a, self.me, label="Go on", instance="sketchup-1")
        self.assertEqual(self.switches, [(9877, self.a)])
        self.assertIn(f"now it loads from {self.a}", record["message"])
        r = self.records()[0]
        self.assertEqual(r["env"]["SHELLIFY_SRC"], self.a)
        self.assertEqual(r["pid"], 101)
        self.assertEqual(r["lease"]["holder_pid"], self.me[0])

    def test_an_instance_another_session_holds_is_refused(self):
        self.pool({"pid": 101, "lease": self.held(self.other, "Posts")})
        with self.assertRaisesRegex(PoolError, 'sketchup-1 is leased to "Posts".*release_sketchup'):
            launcher.lease(self.a, self.me, instance="sketchup-1")
        self.assertEqual(self.records()[0]["lease"]["holder_pid"], self.other[0])

    def test_one_that_isnt_running_is_refused(self):
        self.pool({"last_lease": self.last("Canopy")})
        with self.assertRaisesRegex(PoolError, "isn't running"):
            launcher.lease(self.a, self.me, instance="Canopy")
        self.assertIsNone(self.records()[0]["lease"])

    def test_naming_the_one_it_holds_keeps_the_lease(self):
        self.pool({"pid": 101, "lease": self.held(self.me, "Mine")})
        record = launcher.lease(self.a, self.me, instance="sketchup-1")
        self.assertIn("as you left it", record["message"])
        r = self.records()[0]
        self.assertEqual(r["lease"]["label"], "Mine")
        self.assertEqual(r["lease"]["leased_at"], "2026-11-06T10:00:00+00:00")

    def test_the_holders_other_lease_is_released(self):
        self.pool({"pid": 101, "lease": self.held(self.me, "Mine")}, {"pid": 102})
        launcher.lease(self.a, self.me, instance="sketchup-2")
        first, second = self.records()[:2]
        self.assertIsNone(first["lease"])
        self.assertEqual(first["last_lease"]["label"], "Mine")
        self.assertEqual(second["lease"]["holder_pid"], self.me[0])

    def test_a_switch_that_cant_be_done_leaves_it_free(self):
        self.pool({"pid": 101, "src": self.b, "last_lease": self.last("Canopy")})
        self.switch_result = PoolError("its dev loader is too old")
        with self.assertRaisesRegex(PoolError, "Couldn't switch sketchup-1.*too old"):
            launcher.lease(self.a, self.me, instance="sketchup-1")
        r = self.records()[0]
        self.assertIsNone(r["lease"])
        self.assertEqual(r["env"]["SHELLIFY_SRC"], self.b)
        # Still findable by who used it.
        self.assertEqual(r["last_lease"]["label"], "Canopy")

    def test_a_reload_that_raised_records_the_new_source_and_leaves_it_free(self):
        self.pool({"pid": 101, "src": self.b})
        self.switch_result = (self.a, "SyntaxError: shell.rb:3")
        with self.assertRaisesRegex(PoolError, "failed while reloading.*SyntaxError"):
            launcher.lease(self.a, self.me, instance="sketchup-1")
        r = self.records()[0]
        self.assertIsNone(r["lease"])
        self.assertEqual(r["env"]["SHELLIFY_SRC"], self.a)

    def test_src_is_checked_first(self):
        self.pool({"pid": 101})
        with self.assertRaisesRegex(PoolError, "no shellify.rb or autoclad.rb"):
            launcher.lease(str(self.dir), self.me, instance="sketchup-1")

    def test_an_autoclad_instance_is_leased_by_name_on_the_folder_it_runs(self):
        autoclad = self.src("autoclad-src", "autoclad.rb")
        self.pool({"pid": 101, "src": autoclad})
        record = launcher.lease(autoclad, self.me, instance="sketchup-1")
        self.assertIn("loads AutoClad from this source; call AutoClad::SketchUpUI::Dev.reload", record["message"])
        self.assertEqual(self.switches, [])

    def test_no_switch_to_or_from_an_autoclad_folder(self):
        autoclad = self.src("autoclad-src", "autoclad.rb")
        for runs, wanted in [(self.a, autoclad), (autoclad, self.a)]:
            self.pool({"pid": 101, "src": runs})
            with self.assertRaisesRegex(PoolError, "only Shellify's dev loader can switch"):
                launcher.lease(wanted, self.me, instance="sketchup-1")
            r = self.records()[0]
            self.assertIsNone(r["lease"])
            self.assertEqual(launcher.record_src(r), runs)
        self.assertEqual(self.switches, [])


class AutoCladSourceTest(PoolTest):
    """AutoClad's src folder (with autoclad.rb) is leased like Shellify's, and
    passed to SketchUp as AUTOCLAD_SRC, so AutoClad's tests can run in a pool
    SketchUp on Windows."""

    def test_an_autoclad_folder_is_accepted(self):
        autoclad = self.src("autoclad-src", "autoclad.rb")
        self.assertEqual(launcher.check_src(autoclad), autoclad)
        self.assertEqual(launcher.source_of(autoclad)[1:3], ("AUTOCLAD_SRC", "AutoClad"))

    def test_a_fresh_lease_records_autoclad_src(self):
        autoclad = self.src("autoclad-src", "autoclad.rb")
        self.pool({})
        with (
            mock.patch.object(launcher, "_kill"),
            mock.patch.object(launcher, "_launch", return_value=(105, 105.0)) as launch,
            mock.patch.object(launcher, "_wait_ready"),
        ):
            self.running.add(105)
            record = launcher.lease(autoclad, self.me, label="AutoClad tests")
        self.assertTrue(record["fresh"])
        self.assertIn(f"loading AutoClad from {autoclad}", record["message"])
        self.assertEqual(launch.call_args.args[1], autoclad)
        r = self.records()[0]
        self.assertEqual(r["env"], {"AUTOCLAD_SRC": autoclad})
        self.assertEqual(launcher.record_src(r), autoclad)

    def test_an_instance_on_shellify_is_restarted_for_autoclad(self):
        autoclad = self.src("autoclad-src", "autoclad.rb")
        self.pool(
            {"pid": 101, "src": self.a},
            {"pid": 102, "lease": self.held(self.other)},
            {"pid": 103, "lease": self.held((3, 3.0))},
            {"pid": 104, "lease": self.held((4, 4.0))},
        )
        self.running |= {3, 4}
        with (
            mock.patch.object(launcher, "_kill") as kill,
            mock.patch.object(launcher, "_launch", return_value=(105, 105.0)),
            mock.patch.object(launcher, "_wait_ready"),
        ):
            self.running.add(105)
            launcher.lease(autoclad, self.me)
        kill.assert_called_once_with(101, 101.0)
        self.assertEqual(self.records()[0]["env"], {"AUTOCLAD_SRC": autoclad})

    def test_a_running_autoclad_instance_is_reused_for_the_same_folder(self):
        autoclad = self.src("autoclad-src", "autoclad.rb")
        self.pool({"pid": 101, "src": autoclad})
        record = launcher.lease(autoclad, self.me)
        self.assertFalse(record["fresh"])
        self.assertIn("call AutoClad::SketchUpUI::Dev.reload", record["message"])


class SwitchSrcTest(unittest.TestCase):
    def test_the_snippet_names_src_as_a_ruby_string(self):
        self.assertEqual(launcher._ruby_string("C:/it's/a\\b"), "'C:/it\\'s/a\\\\b'")

    def test_replies(self):
        cases = [
            ('{"src":"C:/a","error":null}', ("C:/a", None)),
            ('{"src":"C:/a","error":"NameError: x"}', ("C:/a", "NameError: x")),
        ]
        for text, expected in cases:
            with mock.patch.object(launcher, "eval_ruby", return_value=text) as ev:
                self.assertEqual(launcher.switch_src(9877, "C:/a"), expected)
                port, code, _ = ev.call_args.args
                self.assertEqual(port, 9877)
                self.assertIn("Shellify.sync('C:/a')", code)
        for text, message in [
            ('{"old_loader":true}', "dev loader that can't switch"),
            ('{"error":"NoMethodError: y"}', "NoMethodError: y"),
            ("nonsense", "unexpected reply"),
        ]:
            with mock.patch.object(launcher, "eval_ruby", return_value=text):
                with self.assertRaisesRegex(PoolError, message):
                    launcher.switch_src(9877, "C:/a")


if __name__ == "__main__":
    unittest.main()
