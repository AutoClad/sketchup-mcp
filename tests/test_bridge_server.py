"""The Ruby socket server inside SketchUp (su_mcp/su_mcp/main.rb), driven
outside SketchUp by tests/bridge_harness.rb. It runs on SketchUp's main
thread and keeps its clients across ticks: a client that is silent, slow to
send or sends something broken must not keep the others from being served."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "bridge_harness.rb"
SERVER = ROOT / "su_mcp" / "su_mcp" / "main.rb"


def no_ruby():
    """Why these can't run with the ruby on the path, or None. The harness
    is written for Ruby 3 (SketchUp's is 3.2), and macOS's own is 2.6."""
    if shutil.which("ruby") is None:
        return "needs ruby on PATH"
    try:
        asked = subprocess.run(["ruby", "-e", "print RUBY_VERSION"], capture_output=True, text=True, timeout=60)
        version = asked.stdout.strip()
        if int(version.split(".")[0]) >= 3:
            return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        version = "one that doesn't run"
    return f"needs Ruby 3 or later; the ruby on PATH is {version}"


pytestmark = pytest.mark.skipif(no_ruby() is not None, reason=str(no_ruby()))


@pytest.fixture(scope="module")
def observed():
    run = subprocess.run(["ruby", str(HARNESS), str(SERVER)], capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_a_silent_client_does_not_hold_up_the_next_request(observed):
    assert observed["served_while_silent_waits"] == "42"


def test_a_client_keeps_its_connection_for_more_requests(observed):
    assert observed["second_request_on_same_socket"] == "5"


def test_a_client_still_sending_is_served_once_its_request_is_complete(observed):
    assert observed["slow_sender_served"] == "2"


def test_a_handler_error_outside_standard_error_still_gets_an_answer(observed):
    # SyntaxError is a ScriptError, not a StandardError.
    assert observed["syntax_error_answered"] is True
    assert observed["usable_after_syntax_error"] == "ok"


def test_an_oversized_request_is_dropped(observed):
    assert observed["oversized_dropped"] is True


def test_a_slow_but_steady_reader_gets_its_whole_reply(observed):
    assert observed["steady_reader_got_whole_reply"] is True


def test_a_tool_call_during_another_request_is_refused_not_run(observed):
    assert observed["outer_finished"] == "outer_done"
    assert observed["nested_call_refused_busy"] is True
    assert observed["nested_call_not_run"] is True


def test_server_status_is_answered_while_a_request_runs(observed):
    assert observed["status_answered_while_busy"] is True


def test_one_request_per_connection_still_works_and_leaves_nothing_behind(observed):
    assert observed["one_shot_served"] == "once"
    assert observed["clients_left"] == 0
