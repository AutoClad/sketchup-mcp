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
    assert observed["good_closed_after_reply"] is True


def test_silent_and_half_sent_clients_are_dropped_after_the_idle_timeout(observed):
    assert observed["silent_dropped"] is True
    assert observed["partial_dropped"] is True


def test_a_client_still_sending_is_not_dropped(observed):
    assert observed["slow_sender_served"] == "2"


def test_a_handler_error_outside_standard_error_still_gets_an_answer(observed):
    # SyntaxError is a ScriptError, not a StandardError.
    assert observed["syntax_error_answered"] is True
    assert observed["syntax_error_closed"] is True


def test_an_oversized_request_is_dropped(observed):
    assert observed["oversized_dropped"] is True


def test_a_reader_that_never_reads_cannot_block_a_tick(observed):
    assert observed["slowest_tick_ms_with_stuck_reader"] < 50
    assert observed["stuck_reader_dropped"] is True


def test_nothing_is_left_pending(observed):
    assert observed["pending_left"] == 0
