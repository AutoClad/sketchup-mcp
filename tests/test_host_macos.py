"""The pool's macOS code (sketchup_mcp/host/macos.py) against the system
itself: real processes, the real lock, and a real program started in
SketchUp's place, up to a whole lease. Nothing here starts SketchUp. It runs
on macOS and, but for what starts a program with macOS's `open`, on Linux,
which uses the same code; CI's macOS job runs it."""

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the pool's code for macOS")

# For the tests that start the stand-in for SketchUp.
needs_open = pytest.mark.skipif(sys.platform != "darwin", reason="starts a program with macOS's open")

if sys.platform != "win32":
    from sketchup_mcp import launcher
    from sketchup_mcp.host import macos
    from sketchup_mcp.launcher import PoolError

# Stands in for SketchUp with the extension loaded: the program of an app
# bundle, started by `open`. It writes what it was started with into the
# pool's folder (seen-<port>), then listens on SKETCHUP_MCP_PORT and answers
# every request with its pid, as the extension answers
# eval_ruby("Process.pid"). The model `open` was given reaches a real app as
# an event, not as an argument, so this never learns of it.
FAKE_SKETCHUP = """#!{python}
import json, os, socket, sys
seen = os.path.join(os.path.dirname(os.environ["SKETCHUP_MCP_POOL_FILE"]), "seen-" + os.environ["SKETCHUP_MCP_PORT"])
with open(seen + ".tmp", "w") as f:
    json.dump({{"pid": os.getpid(), "ppid": os.getppid(), "env": dict(os.environ), "argv": sys.argv[1:]}}, f)
os.replace(seen + ".tmp", seen)
server = socket.create_server(("127.0.0.1", int(os.environ["SKETCHUP_MCP_PORT"])))
while True:
    conn, _ = server.accept()
    with conn:
        conn.recv(65536)
        reply = {{"jsonrpc": "2.0", "id": 1, "result": {{"content": [{{"type": "text", "text": str(os.getpid())}}]}}}}
        conn.sendall(json.dumps(reply).encode() + b"\\n")
"""

# What makes the bundle an app to `open`. LSUIElement keeps it out of the
# Dock and from taking the front.
INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>SketchUp</string>
<key>CFBundleIdentifier</key><string>test.sketchup-mcp.stand-in</string>
<key>CFBundleName</key><string>Fake SketchUp</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
"""

# Ignores a request to quit, as SketchUp does with an unsaved change.
STUBBORN = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(120)"
)


def wait_for(condition, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def free_ports(count):
    held = [socket.create_server(("127.0.0.1", 0)) for _ in range(count)]
    ports = [s.getsockname()[1] for s in held]
    for s in held:
        s.close()
    return ports


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield proc
    proc.kill()
    proc.wait()


@pytest.fixture
def fake_app(tmp_path):
    """A program to start in SketchUp's place, laid out as an app bundle."""
    program = tmp_path / "Fake SketchUp.app" / "Contents" / "MacOS" / "SketchUp"
    program.parent.mkdir(parents=True)
    (program.parent.parent / "Info.plist").write_text(INFO_PLIST)
    program.write_text(FAKE_SKETCHUP.format(python=sys.executable))
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return program


@pytest.fixture
def pool(tmp_path, fake_app, monkeypatch):
    """A pool of its own in tmp_path, on four free ports, whose SketchUp is
    the fake one. Every program it started is killed afterwards."""
    folder = tmp_path / "pool"
    folder.mkdir()
    template = tmp_path / "template.skp"
    template.write_bytes(b"an empty model")
    monkeypatch.setattr(launcher, "POOL_DIR", folder)
    monkeypatch.setattr(launcher, "POOL_FILE", folder / "pool.json")
    monkeypatch.setattr(launcher, "LOCK_FILE", folder / "pool.lock")
    monkeypatch.setattr(launcher, "MODELS_DIR", folder / "models")
    monkeypatch.setattr(launcher, "PORTS", free_ports(4))
    monkeypatch.setattr(launcher, "SKETCHUP_EXE", fake_app)
    monkeypatch.setattr(launcher, "TEMPLATE", template)
    monkeypatch.setattr(launcher, "START_TIMEOUT", 30.0)
    yield folder
    for record in launcher._load():
        if launcher.is_running(record["pid"], record["started"]):
            os.kill(record["pid"], 9)
    # And any the pool has no record of.
    for port in launcher.PORTS:
        for pid in macos._instances(fake_app, port):
            os.kill(pid, 9)


def source(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "shellify.rb").write_text("# Shellify")
    return launcher.normalize_src(str(folder))


def seen_by(record):
    """What the fake SketchUp of a pool record was started with."""
    path = launcher.POOL_DIR / f"seen-{record['port']}"
    assert wait_for(path.exists), "the program never ran"
    return json.loads(path.read_text())


# --- Processes ---------------------------------------------------------------


def test_a_running_process_has_a_start_time_that_stays_the_same(sleeper):
    started = macos.process_started(sleeper.pid)

    assert abs(started - time.time()) < 30
    assert macos.process_started(sleeper.pid) == started
    assert launcher.is_running(sleeper.pid, started)
    assert not launcher.is_running(sleeper.pid, started - 5)


def test_a_start_time_doesnt_depend_on_the_time_zone_or_language(sleeper, monkeypatch):
    started = macos.process_started(sleeper.pid)

    monkeypatch.setenv("TZ", "Pacific/Auckland")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")

    assert macos.process_started(sleeper.pid) == started


def test_a_process_that_has_gone_has_no_start_time(sleeper):
    sleeper.kill()
    sleeper.wait()

    assert macos.process_started(sleeper.pid) is None
    assert macos.process_started(0) is None
    assert macos.process_started(None) is None


def test_a_process_that_ended_but_wasnt_collected_isnt_running(sleeper):
    # A zombie: still in the process table until its parent waits for it.
    sleeper.kill()
    assert wait_for(
        lambda: (
            "Z" in subprocess.run(["ps", "-o", "stat=", "-p", str(sleeper.pid)], capture_output=True, text=True).stdout
        )
    )

    assert macos.process_started(sleeper.pid) is None


def test_the_process_table_has_this_process_and_its_parent(sleeper):
    table = macos.processes()

    parent, name = table[sleeper.pid]
    assert parent == os.getpid()
    assert name.lower() in macos.PYTHON_LAUNCHERS
    assert os.path.isabs(macos.image_path(sleeper.pid))
    assert "python" in os.path.basename(macos.image_path(sleeper.pid)).lower()


def test_this_processs_holder_is_found():
    pid, started = launcher.holder()

    assert pid == os.getpid()
    assert launcher.is_running(pid, started)
    # The shell (or whatever else) that ran the tests, past Python and uv.
    shell = launcher.calling_shell()
    assert shell != os.getpid()
    assert launcher.process_started(shell) is not None


def test_this_server_isnt_taken_for_one_the_desktop_app_shares():
    assert launcher.shared_server() is None


# --- Whose MCP server it is --------------------------------------------------

APP = "/Applications/Claude.app/Contents/MacOS/Claude"
HELPER = "/Applications/Claude.app/Contents/Helpers/disclaimer"
CODE = (
    "/Users/someone/Library/Application Support/Claude/claude-code/2.1.330/4262a1273231"
    "/claude.app/Contents/MacOS/claude"
)


def shared(*chain, paths):
    """Whether the server at the start of chain, a list of program names
    from the server up to its farthest ancestor, is shared; paths gives full
    paths by pid (pids are 1, 2, ... along the chain)."""
    table = {i + 1: (i + 2 if i + 1 < len(chain) else 0, name) for i, name in enumerate(chain)}
    return launcher.shared_server(1, table, lambda pid: paths.get(pid))


def test_a_server_the_desktop_app_starts_is_shared():
    assert "claude_desktop_config.json" in shared("python3.10", "uv", "Claude", "launchd", paths={3: APP})
    assert shared("Python", "disclaimer", "Claude", paths={2: HELPER, 3: APP}) is not None


def test_a_server_a_sessions_claude_code_starts_isnt():
    # As seen on a Mac: python, started by uv, started by the session's Claude Code.
    chain = ("python", "uv", "claude", "disclaimer", "Claude", "launchd")
    assert shared(*chain, paths={3: CODE, 4: HELPER, 5: APP}) is None


def test_a_server_run_from_a_terminal_isnt():
    assert shared("python3.12", "uvx", "zsh", "Terminal", paths={3: "/bin/zsh"}) is None


# --- The lock and the folder -------------------------------------------------


def test_the_lock_is_held_by_one_file_handle_at_a_time(tmp_path):
    path = tmp_path / "pool.lock"
    with open(path, "a+b") as first, open(path, "a+b") as second:
        macos.lock(first)
        with pytest.raises(OSError):
            macos.lock(second)
        macos.unlock(first)
        macos.lock(second)
        macos.unlock(second)


def test_the_lock_keeps_another_process_out(tmp_path, pool):
    code = (
        "import sys, time; sys.path.insert(0, sys.argv[1]); "
        "from sketchup_mcp.host import macos; "
        "f = open(sys.argv[2], 'a+b'); macos.lock(f); print('locked', flush=True); time.sleep(120)"
    )
    src = str(Path(launcher.__file__).resolve().parent.parent)
    other = subprocess.Popen([sys.executable, "-c", code, src, str(launcher.LOCK_FILE)], stdout=subprocess.PIPE)
    try:
        assert other.stdout.readline().strip() == b"locked"
        with open(launcher.LOCK_FILE, "a+b") as mine, pytest.raises(OSError):
            macos.lock(mine)
    finally:
        other.kill()
        other.wait()
    # A lock dies with its holder.
    assert [r["name"] for r in launcher.status()] == ["sketchup-1", "sketchup-2", "sketchup-3", "sketchup-4"]


def test_the_pools_folder_is_made_when_its_missing(tmp_path, monkeypatch):
    folder = tmp_path / "Application Support" / "sketchup-mcp"
    monkeypatch.setattr(launcher, "POOL_DIR", folder)
    monkeypatch.setattr(launcher, "POOL_FILE", folder / "pool.json")
    monkeypatch.setattr(launcher, "LOCK_FILE", folder / "pool.lock")

    assert len(launcher.status()) == 4
    assert (folder / "pool.json").is_file()


# --- Ending SketchUp ---------------------------------------------------------


def test_kill_asks_a_process_to_quit(sleeper):
    started = macos.process_started(sleeper.pid)
    began = time.time()

    macos.kill(sleeper.pid)

    assert time.time() - began < macos.QUIT_WAIT
    # A child of the tests, so it has to be collected; the pool's aren't.
    assert sleeper.wait(5) == -15
    assert not launcher.is_running(sleeper.pid, started)


def test_kill_ends_a_process_that_ignores_the_request(monkeypatch):
    monkeypatch.setattr(macos, "QUIT_WAIT", 0.5)
    stubborn = subprocess.Popen([sys.executable, "-c", STUBBORN], stdout=subprocess.PIPE)
    try:
        assert stubborn.stdout.readline().strip() == b"ready"
        started = macos.process_started(stubborn.pid)

        launcher._kill(stubborn.pid, started)

        assert stubborn.wait(5) == -9
    finally:
        stubborn.kill()


def test_killing_a_process_that_has_gone_does_nothing(sleeper):
    sleeper.kill()
    sleeper.wait()

    macos.kill(sleeper.pid)


# --- Starting SketchUp -------------------------------------------------------


@needs_open
def test_start_opens_the_app_on_a_copy_of_the_template_with_its_port_and_source(pool, fake_app, monkeypatch):
    # Set for this process, as in a server started from a pool instance: not passed on.
    monkeypatch.setenv("SKETCHUP_MCP_PORT", "1")
    monkeypatch.setenv("SKETCHUP_MCP_BIND", "0.0.0.0")
    monkeypatch.setenv("SHELLIFY_SRC", "/somewhere/else")
    record = launcher._blank(launcher.PORTS[0])
    stale = [Path(record["model"]).with_name("AutoSave_pool-sketchup-1.skp"), Path(record["model"]).with_suffix(".skb")]
    Path(record["model"]).parent.mkdir(parents=True)
    for path in stale:
        path.write_text("left by the last one")
    commands = []
    run = subprocess.run

    def recording(command, **kwargs):
        if command[0] == "open":
            commands.append(command)
        return run(command, **kwargs)

    monkeypatch.setattr(macos.subprocess, "run", recording)

    pid, started = launcher._launch(record, "/work/a tree")
    record.update(pid=pid, started=started)
    launcher._save([record])

    seen = seen_by(record)
    assert seen["pid"] == pid
    assert launcher.is_running(pid, started)
    # A new SketchUp, without the windows of the last one, on the model, and
    # not in the background (-g), where SketchUp loads no extensions.
    (command,) = commands
    assert command[:5] == ["open", "-n", "-F", "-a", str(fake_app.parent.parent.parent)]
    assert command[-1] == record["model"]
    assert "-g" not in command
    assert Path(record["model"]).read_bytes() == b"an empty model"
    assert not any(path.exists() for path in stale)
    assert seen["env"]["SKETCHUP_MCP_PORT"] == str(launcher.PORTS[0])
    assert seen["env"]["SHELLIFY_SRC"] == "/work/a tree"
    assert seen["env"]["SKETCHUP_MCP_POOL_FILE"] == str(launcher.POOL_FILE)
    assert "SKETCHUP_MCP_BIND" not in seen["env"]
    # It belongs to no MCP server: not this process's child, so it outlives
    # it, and leaves no zombie here when it ends.
    assert seen["ppid"] != os.getpid()
    assert launcher.ask_pid(launcher.PORTS[0]) == pid

    launcher._kill(pid, started)
    assert not launcher.is_running(pid, started)


def test_an_instance_is_found_by_its_program_and_the_port_in_its_environment(tmp_path):
    program = tmp_path / "Some.app" / "Contents" / "MacOS" / "SketchUp"
    program.parent.mkdir(parents=True)
    program.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
    program.chmod(0o755)
    port, other = free_ports(2)

    def run(command, port):
        return subprocess.Popen(command, env={**os.environ, "SKETCHUP_MCP_PORT": str(port)})

    started = [
        run([str(program), "a model.skp"], port),
        # The same program for another port, and another program that has
        # the port in its environment, and both in its arguments.
        run([str(program)], other),
        run([sys.executable, "-c", "import time; time.sleep(120)", str(program), f"SKETCHUP_MCP_PORT={port}"], port),
    ]
    try:
        assert wait_for(lambda: macos._instances(program, port) == [started[0].pid]), macos._instances(program, port)
        assert macos._instances(program, other) == [started[1].pid]
        assert macos._instances(program, free_ports(1)[0]) == []
    finally:
        for proc in started:
            proc.kill()
            proc.wait()


@needs_open
def test_a_sketchup_the_pool_has_no_record_of_is_ended_before_another_starts_on_its_port(pool):
    # As when the process that started it ended before writing it down.
    record = launcher._blank(launcher.PORTS[0])
    lost, lost_started = launcher._launch(record, "/work/tree")
    seen_by(record)
    (launcher.POOL_DIR / f"seen-{record['port']}").unlink()
    # It listens, so the launcher itself would refuse the port: straight to start.
    pid, started = macos.start(record, "/work/tree", launcher.SKETCHUP_EXE, launcher.TEMPLATE, pool, launcher.POOL_FILE)
    record.update(pid=pid, started=started)
    launcher._save([record])

    assert pid != lost
    assert not launcher.is_running(lost, lost_started)
    assert seen_by(record)["pid"] == pid


@needs_open
def test_a_program_that_cant_be_run_is_reported(pool, fake_app):
    fake_app.chmod(0o644)

    with pytest.raises(PoolError, match="Starting SketchUp failed: open said: .+"):
        launcher._launch(launcher._blank(launcher.PORTS[0]), "/work/tree")


@needs_open
def test_a_program_that_exits_straight_away_is_reported(pool, fake_app, monkeypatch):
    fake_app.write_text("#!/bin/sh\nexit 3\n")
    monkeypatch.setattr(macos, "FIND_WAIT", 1.0)

    with pytest.raises(PoolError, match="exited straight away"):
        launcher._launch(launcher._blank(launcher.PORTS[0]), "/work/tree")


def test_a_program_outside_an_app_bundle_is_refused(pool, tmp_path, monkeypatch):
    program = tmp_path / "SketchUp"
    program.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launcher, "SKETCHUP_EXE", program)
    monkeypatch.setattr(macos.sys, "platform", "darwin")

    with pytest.raises(PoolError, match="isn't inside an app bundle"):
        launcher._launch(launcher._blank(launcher.PORTS[0]), "/work/tree")


def test_nothing_is_started_where_there_is_no_sketchup(pool, monkeypatch):
    monkeypatch.setattr(macos.sys, "platform", "linux")

    with pytest.raises(PoolError, match="no SketchUp to start on this system"):
        launcher._launch(launcher._blank(launcher.PORTS[0]), "/work/tree")


def test_a_missing_program_is_reported_before_anything_starts(pool, fake_app):
    fake_app.unlink()

    with pytest.raises(PoolError, match="SketchUp isn't at .*set SKETCHUP_EXE"):
        launcher._launch(launcher._blank(launcher.PORTS[0]), "/work/tree")


# --- A whole lease -----------------------------------------------------------


@needs_open
def test_a_lease_starts_sketchup_keeps_it_for_the_next_and_restarts_it_for_another_source(pool, tmp_path):
    a, b = source(tmp_path, "a"), source(tmp_path, "b")
    me = launcher.holder()

    first = launcher.lease(a, me, label="First")

    assert first["fresh"] is True and first["name"] == "sketchup-1"
    assert launcher.is_running(first["pid"], first["started"])
    assert seen_by(first)["env"]["SHELLIFY_SRC"] == a
    assert launcher.held(me)["alive"] is True

    again = launcher.lease(a, me)
    assert again["fresh"] is False and again["pid"] == first["pid"]

    assert launcher.release(me)["name"] == "sketchup-1"
    assert launcher.is_running(first["pid"], first["started"]), "released, it keeps running"

    # Free and already on this source: taken as it is.
    reused = launcher.lease(a, me)
    assert reused["fresh"] is False and reused["pid"] == first["pid"]
    launcher.release(me)

    # Another source: one that isn't running is started for it.
    other = launcher.lease(b, me, label="Other")
    assert other["fresh"] is True and other["name"] == "sketchup-2"
    assert seen_by(other)["env"]["SHELLIFY_SRC"] == b
    assert launcher.is_running(first["pid"], first["started"])


@needs_open
def test_a_leased_sketchup_that_died_is_seen_and_replaced(pool, tmp_path):
    a = source(tmp_path, "a")
    me = launcher.holder()
    first = launcher.lease(a, me)

    os.kill(first["pid"], 9)
    assert wait_for(lambda: not launcher.is_running(first["pid"], first["started"]))

    assert launcher.held(me)["alive"] is False
    second = launcher.lease(a, me)
    assert second["fresh"] is True and second["pid"] != first["pid"]
    assert launcher.is_running(second["pid"], second["started"])


@needs_open
def test_a_sketchup_that_never_listens_fails_the_lease_and_frees_it(pool, tmp_path, fake_app, monkeypatch):
    # As when SketchUp stops at a dialog before its extensions load.
    fake_app.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
    monkeypatch.setattr(launcher, "START_TIMEOUT", 2.0)
    me = launcher.holder()

    with pytest.raises(PoolError, match="didn't answer on port"):
        launcher.lease(source(tmp_path, "a"), me)

    record = launcher.status()[0]
    assert record["lease"] is None
    assert record["alive"] is True  # left running, for whoever looks at what stopped it


@needs_open
def test_after_a_sketchup_that_never_listened_the_next_lease_ends_it_and_starts_another(
    pool, tmp_path, fake_app, monkeypatch
):
    good = fake_app.read_text()
    fake_app.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
    monkeypatch.setattr(launcher, "START_TIMEOUT", 2.0)
    monkeypatch.setattr(launcher, "PORTS", launcher.PORTS[:1])
    a, me = source(tmp_path, "a"), launcher.holder()
    with pytest.raises(PoolError, match="didn't answer on port"):
        launcher.lease(a, me)
    stuck = launcher.status()[0]

    fake_app.write_text(good)
    monkeypatch.setattr(launcher, "START_TIMEOUT", 30.0)
    after = launcher.lease(a, me)

    assert after["fresh"] is True and after["name"] == "sketchup-1"
    assert not launcher.is_running(stuck["pid"], stuck["started"])
    assert launcher.ask_pid(after["port"]) == after["pid"]


@needs_open
def test_a_lease_whose_holder_has_gone_is_freed(pool, tmp_path):
    a = source(tmp_path, "a")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        record = launcher.lease(a, launcher.holder(holder.pid), label="A session")
        assert launcher.status()[0]["lease"]["label"] == "A session"
    finally:
        holder.kill()
        holder.wait()

    after = launcher.status()[0]
    assert after["lease"] is None
    assert after["last_lease"]["label"] == "A session"
    assert launcher.is_running(record["pid"], record["started"])


# --- The CLI -----------------------------------------------------------------


def test_the_package_loads_and_the_cli_runs_with_nothing_put_in_its_place(tmp_path):
    # In a process of its own: no conftest, so no stand-ins.
    src = str(Path(launcher.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src, "HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}

    loaded = subprocess.run(
        [sys.executable, "-c", "import sketchup_mcp.server, sketchup_mcp.launcher as l; print(l.POOL_FILE)"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip().startswith(str(tmp_path))

    status = subprocess.run(
        [sys.executable, "-m", "sketchup_mcp.launcher", "status"], env=env, capture_output=True, text=True
    )
    assert status.returncode == 0, status.stderr
    assert [r["port"] for r in json.loads(status.stdout)] == [9877, 9878, 9879, 9880]
    assert all(r["alive"] is False for r in json.loads(status.stdout))
