"""The Ruby socket server inside SketchUp (su_mcp/su_mcp/main.rb), driven
outside SketchUp by tests/bridge_harness.rb. It runs on SketchUp's main
thread, so the property that matters most is that nothing a client does can
make it wait: a silent client, a slow sender, a reader that never reads, a
broken request."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "bridge_harness.rb"
SERVER = ROOT / "su_mcp" / "su_mcp" / "main.rb"

pytestmark = pytest.mark.skipif(shutil.which("ruby") is None, reason="needs ruby on PATH")


@pytest.fixture(scope="module")
def observed():
    run = subprocess.run(["ruby", str(HARNESS), str(SERVER)], capture_output=True, text=True, timeout=120)
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


def test_a_reader_that_never_reads_cannot_block_a_tick(observed):
    assert observed["slowest_tick_ms_with_stuck_reader"] < 50
    assert observed["stuck_reader_dropped"] is True


def test_one_request_per_connection_still_works_and_leaves_nothing_behind(observed):
    assert observed["one_shot_served"] == "once"
    assert observed["clients_left"] == 0
