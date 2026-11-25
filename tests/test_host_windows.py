"""The pool's Windows code (sketchup_mcp/host/windows.py) against Windows
itself: real processes, the real lock, and spawn starting a real program in
SketchUp's place. Nothing here starts SketchUp or creates a scheduled task.
Windows only; CI's Windows job runs it."""

import json
import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="calls Windows' own APIs")

if sys.platform == "win32":
    from sketchup_mcp import launcher
    from sketchup_mcp.host import windows

# Stands in for SketchUp: spawn runs `<exe> <model>`, so with Python as the
# exe and this as the template, the copy of it is what runs. It writes what
# it was started with, then stays until it's killed.
FAKE_SKETCHUP = """
import json, os, sys, time
with open(sys.argv[0] + ".seen.tmp", "w") as f:
    json.dump({"pid": os.getpid(), "env": dict(os.environ), "argv": sys.argv}, f)
os.replace(sys.argv[0] + ".seen.tmp", sys.argv[0] + ".seen")
time.sleep(120)
"""


def wait_for(condition, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield proc
    proc.kill()
    proc.wait()


def test_a_running_process_has_a_start_time_that_stays_the_same(sleeper):
    started = windows.process_started(sleeper.pid)

    assert abs(started - time.time()) < 30
    assert windows.process_started(sleeper.pid) == started
    assert launcher.is_running(sleeper.pid, started)
    assert not launcher.is_running(sleeper.pid, started - 5)


def test_a_process_that_has_gone_has_no_start_time(sleeper):
    sleeper.kill()
    sleeper.wait()

    assert windows.process_started(sleeper.pid) is None
    assert windows.process_started(0) is None


def test_the_process_table_has_this_process_and_its_parent(sleeper):
    table = windows.processes()

    assert table[sleeper.pid][0] == os.getpid()
    assert table[sleeper.pid][1].lower() in windows.PYTHON_LAUNCHERS
    assert windows.image_path(sleeper.pid).lower().endswith("python.exe")


def test_kill_ends_a_process(sleeper):
    started = windows.process_started(sleeper.pid)

    launcher._kill(sleeper.pid, started)

    assert not launcher.is_running(sleeper.pid, started)


def test_the_lock_is_held_by_one_file_handle_at_a_time(tmp_path):
    path = tmp_path / "pool.lock"
    with open(path, "a+b") as first, open(path, "a+b") as second:
        windows.lock(first)
        with pytest.raises(OSError):
            windows.lock(second)
        windows.unlock(first)
        windows.lock(second)
        windows.unlock(second)


def test_the_users_environment_is_read_from_windows():
    env = windows.user_environment()

    assert {name.upper() for name in env} >= {"SYSTEMROOT", "PATH", "USERPROFILE"}


def test_the_pools_folder_is_created(tmp_path):
    folder = tmp_path / "made by wmi"

    windows.make_shared_dir(folder)

    assert folder.is_dir()


def test_spawn_starts_the_program_on_a_copy_of_the_template_with_its_port_and_source(tmp_path):
    template = tmp_path / "template.skp"
    template.write_text(FAKE_SKETCHUP)
    model = tmp_path / "models" / "pool-sketchup-1.skp"
    result = tmp_path / "started-sketchup-1.json"
    request = tmp_path / "start-sketchup-1.json"
    request.write_text(
        json.dumps(
            {
                "exe": sys.executable,
                "template": str(template),
                "model": str(model),
                "port": 9877,
                "src": "C:/work/tree",
                "result": str(result),
            }
        ),
        encoding="utf-8",
    )

    windows.spawn(str(request), tmp_path / "pool.json")

    reply = json.loads(result.read_text(encoding="utf-8"))
    assert "error" not in reply, reply
    try:
        seen_file = model.with_name(model.name + ".seen")
        assert wait_for(seen_file.exists), "the program never ran"
        seen = json.loads(seen_file.read_text())
        assert seen["pid"] == reply["pid"]
        assert launcher.is_running(reply["pid"], reply["started"])
        assert seen["argv"] == [str(model)]
        assert model.read_text() == FAKE_SKETCHUP
        assert seen["env"]["SKETCHUP_MCP_PORT"] == "9877"
        assert seen["env"]["SHELLIFY_SRC"] == "C:/work/tree"
        assert seen["env"]["SKETCHUP_MCP_POOL_FILE"] == str(tmp_path / "pool.json")
        # The user's own environment, not just the few variables given here.
        assert "SYSTEMROOT" in {name.upper() for name in seen["env"]}
    finally:
        launcher._kill(reply["pid"], reply["started"])
    assert not launcher.is_running(reply["pid"], reply["started"])


def test_spawn_reports_a_program_that_isnt_there(tmp_path):
    template = tmp_path / "template.skp"
    template.write_text("")
    result = tmp_path / "started.json"
    request = tmp_path / "start.json"
    request.write_text(
        json.dumps(
            {
                "exe": str(tmp_path / "no such.exe"),
                "template": str(template),
                "model": str(tmp_path / "models" / "m.skp"),
                "port": 9877,
                "src": None,
                "result": str(result),
            }
        ),
        encoding="utf-8",
    )

    windows.spawn(str(request), tmp_path / "pool.json")

    assert "error" in json.loads(result.read_text(encoding="utf-8"))
