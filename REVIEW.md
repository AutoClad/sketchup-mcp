# Review guidance for sketchup-mcp

This is a dev tool: the bridge between Claude Code and SketchUp. Two halves,
Python (the MCP server and the pool of SketchUps it leases from) and Ruby
(the SketchUp extension). `CLAUDE.md` describes both.

Flag:
- Changes to the wire protocol (JSON-RPC shape, framing, how
  `result`/`error` are unwrapped) without a matching test in
  `tests/test_connection.py` or `tests/bridge_harness.rb`.
- Anything new that could hang: a socket read without a timeout, a retry
  loop with no bound, and in `su_mcp/` anything that blocks SketchUp's main
  thread (a blocking read, a `sleep`, waiting on a client). Replies are
  already written with a blocking write; don't flag that, but do flag a
  change that adds another.
- A request that could be sent twice. `eval_ruby` must stay at `retries=0`.
- Changes to the pool (`launcher.py`) that could give one session another's
  SketchUp: a lease not tied to its holder's pid and start time, state read
  or written outside `_locked`, the lock held while SketchUp starts, or a
  weaker `shared_server` check.
- A lease that could start or restart a SketchUp another session holds, or
  touch the user's own SketchUp on 9876.
- A test that starts SketchUp, or reads or writes the real `pool.json`.
- `eval_ruby` executes arbitrary Ruby inside SketchUp. Any change that
  widens who can reach the TCP port (binding to 0.0.0.0 by default,
  removing the localhost default) needs a reason in the PR.
- New MCP tools whose description strings are vague; the model reads
  those to decide when to call the tool. The same goes for the server's
  `instructions`.
- mcp SDK version drift: the server targets the 1.x FastMCP API until we
  migrate. A bump past <2 without the migration will break at import.

Do not flag:
- Style in the inherited upstream code. Ruff's config deliberately
  ignores some of it; tighten as files are touched.
- Formatting in the files listed under `[tool.ruff.format]` in
  `pyproject.toml`, which the format check leaves out.
- That the launcher runs on Windows only.
