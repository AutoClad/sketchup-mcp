"""Where tool calls go: the SketchUp this session leased from the pool, and
nowhere else. There is no default SketchUp, and no host or port to set."""

import pytest

from sketchup_mcp import server


def leased(port=9878, alive=True, **more):
    return {
        "name": "sketchup-2",
        "port": port,
        "alive": alive,
        "env": {"SHELLIFY_SRC": "C:/work"},
        "lease": {"label": "Canopy", "session": "s-1", "link": "claude://s-1"},
        **more,
    }


def test_calls_go_to_the_leased_instance(pool):
    pool.record = leased(port=9878)

    conn = server.get_sketchup_connection()

    assert (conn.host, conn.port) == ("127.0.0.1", 9878)


def test_an_instance_keeps_one_connection(pool):
    pool.record = leased()

    assert server.get_sketchup_connection() is server.get_sketchup_connection()


def test_without_a_lease_a_call_fails_and_says_to_lease(pool):
    with pytest.raises(Exception, match="No SketchUp leased. Call lease_sketchup first"):
        server.get_sketchup_connection()
    assert pool.leases == []


def test_a_leased_sketchup_that_died_is_leased_again_and_nothing_is_run(pool):
    pool.record = leased(alive=False)
    pool.new_record = {"message": "Leased sketchup-3 on port 9879: SketchUp started fresh."}

    with pytest.raises(Exception, match="had died, so nothing was run.*Leased sketchup-3 on port 9879"):
        server.get_sketchup_connection()

    # For the same folder and session as before.
    assert pool.leases == [
        {"src": "C:/work", "who": (1, 1.0), "label": "Canopy", "session": "s-1", "link": "claude://s-1"}
    ]


def test_a_leased_sketchup_that_died_and_cant_be_leased_again_says_why(pool):
    pool.record = leased(alive=False)
    pool.new_record = server.launcher.PoolError("All 4 pool instances are leased")

    with pytest.raises(Exception, match="leasing again failed: All 4 pool instances are leased"):
        server.get_sketchup_connection()
