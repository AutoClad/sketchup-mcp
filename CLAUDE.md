# sketchup-mcp

Bridge between Claude Code and a running SketchUp. Python MCP server
(`src/sketchup_mcp/`) talks JSON over TCP to a Ruby extension
(`su_mcp/`) listening on port 9876 inside SketchUp.

## Commands

    uv sync --dev                                   # once, and after lockfile changes
    uv run pytest                                   # unit tests (socket is mocked)
    uvx ruff check . && uvx ruff format --check .   # lint + format check
    cd su_mcp && zip -r ../su_mcp.rbz su_mcp.rb su_mcp/ extension.json   # build the extension

## Protocol

Each request is one newline-terminated JSON-RPC object and each reply one
JSON object. The Python client keeps one socket per SketchUp open across
requests, checks with a peek that SketchUp hasn't closed it before sending,
and never resends an `eval_ruby` that was sent (it may have run). A client
may also send one request per connection and close, as AutoClad's scenario
runner does. The Ruby server runs on SketchUp's main thread (a UI timer),
so it never blocks: it reads whatever has arrived on each tick, writes
replies with `write_nonblock` (dropping a client that hasn't read its reply
within `WRITE_TIMEOUT`), and answers "busy" to a tool call that arrives
while another request is running in a nested event loop.

## The pool

On Windows, each session leases a SketchUp of its own from a pool of four
(ports 9877-9880; `src/sketchup_mcp/launcher.py`, the `lease_sketchup` and
`release_sketchup` tools; see README.md). Elsewhere, or with `SKETCHUP_PORT`
set, tool calls go straight to `SKETCHUP_HOST:SKETCHUP_PORT`
(localhost:9876 by default). The extension reads its port from
`SKETCHUP_MCP_PORT` and its bind address from `SKETCHUP_MCP_BIND`.
`tests/test_launcher.py` runs on Windows only.

## Rules

- Python targets the mcp SDK 1.x (`FastMCP`) until we migrate to 2.x. Keep `mcp<2`.
- Host/port come from the pool's lease or `SKETCHUP_HOST` / `SKETCHUP_PORT`; never hard-code.
- The Ruby server binds 127.0.0.1 by default. 0.0.0.0 only for a VM setup, never as the default.
- Any change to the wire protocol gets a test in `tests/test_connection.py`.
- Nothing in `su_mcp/` may block SketchUp's main thread: no blocking reads,
  no `sleep`, no waiting on a client.
- Prefer adding AutoClad-specific tools over extending the woodworking ones.

## Branching

Never push to `main`. Every change, however small, goes on a branch, through
a PR, and is merged only when the CI checks are green. This is a team
convention rather than a GitHub setting (the free plan does not enforce it on
private repos), so follow it even when nothing stops you. A pre-push hook that
refuses pushes to `main` is available from AutoClad/.github/scripts.

## Before opening a PR

Run `/code-review` in a fresh Claude Code session on the branch, then make
sure `uv run pytest` and both Ruff commands pass.
