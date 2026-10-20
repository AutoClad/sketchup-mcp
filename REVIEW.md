# Review guidance for sketchup-mcp

This is a dev tool: the bridge between Claude Code and a running SketchUp.
Two halves, Python (MCP server) and Ruby (SketchUp extension).

Flag:
- Changes to the wire protocol (JSON-RPC shape, newline framing, how
  `result`/`error` are unwrapped) without a matching test in tests/.
- Anything that could hang: a socket read without a timeout, a retry loop
  with no bound.
- Host/port hard-coded instead of read from SKETCHUP_HOST / SKETCHUP_PORT
  (Python) or the bind setting (Ruby).
- `eval_ruby` executes arbitrary Ruby inside SketchUp. Any change that
  widens who can reach the TCP port (binding to 0.0.0.0 by default,
  removing the localhost default) needs a reason in the PR.
- New MCP tools whose description strings are vague; the model reads
  those to decide when to call the tool.
- mcp SDK version drift: the server targets the 1.x FastMCP API until we
  migrate. A bump past <2 without the migration will break at import.

Do not flag:
- Style in the inherited upstream code. Ruff's config deliberately
  ignores some of it; tighten as files are touched.
