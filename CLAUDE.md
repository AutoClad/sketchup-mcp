# sketchup-mcp

Bridge between Claude Code and a running SketchUp. Python MCP server
(`src/sketchup_mcp/`) talks JSON over TCP to a Ruby extension
(`su_mcp/`) listening on port 9876 inside SketchUp.

## Commands

    uv sync --dev                                   # once, and after lockfile changes
    uv run pytest                                   # unit tests (socket is mocked)
    uvx ruff check . && uvx ruff format --check .   # lint + format check
    cd su_mcp && zip -r ../su_mcp.rbz su_mcp.rb su_mcp/ extension.json   # build the extension

## Rules

- Python targets the mcp SDK 1.x (`FastMCP`) until we migrate to 2.x. Keep `mcp<2`.
- Host/port come from `SKETCHUP_HOST` / `SKETCHUP_PORT`; never hard-code.
- The Ruby server binds 127.0.0.1 by default. 0.0.0.0 only for a VM setup, never as the default.
- Any change to the wire protocol gets a test in `tests/test_connection.py`.
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
