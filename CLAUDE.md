# sketchup-mcp

Bridge between Claude Code and SketchUp. A Python MCP server
(`src/sketchup_mcp/`) talks JSON over TCP to a Ruby extension (`su_mcp/`)
inside SketchUp.

The server has no default SketchUp. It talks only to SketchUps it leases
from a pool of four that it starts itself (`src/sketchup_mcp/launcher.py`,
ports 9877 to 9880). A session calls `lease_sketchup` before any other tool
and `release_sketchup` when its task is done. The README's "A pool of
SketchUps" has the rules.

The pool runs on Windows and macOS. `launcher.py` has its rules, which
are the same on both. What differs (the lock, finding processes, starting
and ending SketchUp, the paths) is in `src/sketchup_mcp/host/`, one module
per system with the same names in each; `host/__init__.py` lists them. The
macOS module has run only against a stand-in program, never a real SketchUp.
The Ruby extension runs wherever SketchUp does, and a SketchUp started by
hand listens on 9876, which the MCP server doesn't use.

## Commands

    uv sync --dev                                   # once, and after lockfile changes
    uv run pytest                                   # the tests; none starts SketchUp
    uvx ruff check . && uvx ruff format --check .   # lint + format check
    cd su_mcp && zip -r ../su_mcp.rbz su_mcp.rb su_mcp/ extension.json   # build the extension

The tests run on any system, and CI runs them on Linux, Windows and
macOS. `tests/test_host_windows.py` and `tests/test_host_macos.py` run each
system's module against the system itself (real processes, the real lock, a
real program started in SketchUp's place), so each runs only where it can;
Linux runs the macOS ones. `tests/conftest.py` puts stand-ins in place of
`msvcrt` and `ctypes.WinDLL` off Windows, so that tests of Windows' rules
can load `host/windows.py` anywhere. The tests of the Ruby server
(`tests/test_bridge_server.py`) need Ruby 3 or later on the path and are skipped
without it.

## Protocol

A request is one JSON-RPC object followed by a newline, and so is a reply.
Both ends read by whole JSON object, not by line.

The Python client keeps one socket per SketchUp and sends a session's
requests down it (`SketchupConnection`). It never connects at startup.
Before each request it peeks at the socket, and reconnects if SketchUp has
closed it. `eval_ruby` and `server_status` are sent once (`retries=0`): a
request that timed out may have run, so it isn't sent again. The other tools
use `send_command`'s default and are resent up to twice.

The Ruby server runs on SketchUp's main thread, on a UI timer (`tick`), and
keeps its clients across ticks. It reads without blocking. It writes a reply
with a blocking `sock.write`, so a client that doesn't read a large reply
holds SketchUp until it does. While a request is running, a tool call from
another client is answered "busy" and not run; `server_status` is still
answered. A watchdog restarts the tick timer if it stops firing.

## Rules

- Python targets the mcp SDK 1.x (`FastMCP`) until we migrate to 2.x. Keep `mcp<2`.
- The MCP server reads no host or port: it talks to 127.0.0.1, at its
  leased instance's port. The extension reads `SKETCHUP_MCP_PORT` and
  `SKETCHUP_MCP_BIND`.
- The Ruby server binds 127.0.0.1 by default. Never make 0.0.0.0 the default.
- A lease belongs to the MCP server's process, so each session needs a
  server of its own. Don't weaken `shared_server`'s refusal.
- Any change to the wire protocol gets a test: `tests/test_connection.py`
  for the Python client, `tests/bridge_harness.rb` for the Ruby server.
- Don't add anything to `su_mcp/` that can block SketchUp's main thread: no
  blocking reads, no `sleep`, no waiting on a client.
- Nothing changes how the pool behaves on Windows unless that is the point
  of the change. `host/windows.py` is `launcher.py`'s Windows code as it
  was; a change for macOS goes in `host/macos.py`, or behind a name both
  modules have.
- A test never starts SketchUp, and never reads or writes the real
  `pool.json`: replace the launcher's functions (the `pool` fixture in
  `tests/conftest.py`, or the patches in `tests/test_launcher.py`).
- The files listed under `[tool.ruff.format]` in `pyproject.toml` are left
  out of the format check. Don't reformat one as part of another change.
- Prefer adding AutoClad-specific tools over extending the woodworking ones.

## Branching

Work happens on `yolo`. Never push to `yolo` or `main`. Every change,
however small, goes on a branch off `yolo`, through a PR into `yolo`, and is
merged by squash only when the CI checks are green. This is a team
convention, not a GitHub setting, so follow it even when nothing stops you.

## Before opening a PR

Run `/code-review` in a fresh Claude Code session on the branch, then make
sure `uv run pytest` and both Ruff commands pass.
