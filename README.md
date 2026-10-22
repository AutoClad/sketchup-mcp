# SketchupMCP - Sketchup Model Context Protocol Integration

SketchupMCP connects Sketchup to Claude AI through the Model Context Protocol (MCP), allowing Claude to directly interact with and control Sketchup. This integration enables prompt-assisted 3D modeling, scene creation, and manipulation in Sketchup.

Big Shoutout to [Blender MCP](https://github.com/ahujasid/blender-mcp) for the inspiration and structure.

## Features

* **Two-way communication**: Connect Claude AI to Sketchup through a TCP socket connection
* **Component manipulation**: Create, modify, delete, and transform components in Sketchup
* **Material control**: Apply and modify materials and colors
* **Scene inspection**: Get detailed information about the current Sketchup scene
* **Selection handling**: Get and manipulate selected components
* **Ruby code evaluation**: Execute arbitrary Ruby code directly in SketchUp for advanced operations

## Components

The system consists of two main components:

1. **Sketchup Extension**: A Sketchup extension that creates a TCP server within Sketchup to receive and execute commands
2. **MCP Server (`sketchup_mcp/server.py`)**: A Python server that implements the Model Context Protocol and connects to the Sketchup extension

## Installation

### Python Packaging

We're using uv so you'll need to ```brew install uv```

### Sketchup Extension

1. Download or build the latest `.rbz` file
2. In Sketchup, go to Window > Extension Manager
3. Click "Install Extension" and select the downloaded `.rbz` file
4. Restart Sketchup

## Usage

### Starting the Connection

The MCP server talks only to SketchUps it leases from its pool (below), which
it starts itself; there is no default SketchUp to connect to. Each pool
instance's extension listens on `127.0.0.1`, at the port the launcher sets in
`SKETCHUP_MCP_PORT`. A SketchUp started by hand listens on 9876 (Extensions >
MCP Server > Start Server retries if the port was taken), but the MCP server
doesn't use it.

Connecting across a VM (SketchUp in the guest, the MCP server on the host,
with `SKETCHUP_MCP_HOST` and `SKETCHUP_MCP_BIND=0.0.0.0`) was dropped with
the pool, which runs the MCP server and SketchUp on the same machine.
`SKETCHUP_MCP_BIND` still sets the extension's bind address. There is no
authentication on the port and no approval prompt before `eval_ruby` runs.

### When it stops answering

Every request is logged to the Ruby Console as `Request #N started` and
`Request #N finished` (or `failed`). If calls start timing out, the last
`started` with no matching end is the one that never came back. **Extensions >
MCP Server > Show Status** prints the connected clients, the requests still in
flight, and when the server last serviced its sockets.

A request that never returns no longer silences the server: while one is
running, other tool calls are answered at once with an error saying what's
running, and not run, since running them inside the first request's
operation crashed SketchUp. `server_status` is the exception: it runs no code in the model, and says what is running and where, so a request that never comes back can still be looked into. A watchdog restarts the server's timer if it
stops firing while no request is running. `eval_ruby` gives up after 50
seconds, so it answers before the MCP client's own timeout does.
`SKETCHUP_MCP_EVAL_TIMEOUT` on the host changes that; keep it under the
client's limit.

### Using with Claude

Configure Claude to use the MCP server by adding the following to your Claude configuration:

```json
    "mcpServers": {
        "sketchup": {
            "command": "uvx",
            "args": [
                "sketchup-mcp"
            ]
        }
    }
```

This will pull the [latest from PyPI](https://pypi.org/project/sketchup-mcp/)

Once connected, Claude can interact with Sketchup using the following capabilities:

#### Tools

* `get_scene_info` - Gets information about the current Sketchup scene
* `get_selected_components` - Gets information about currently selected components
* `create_component` - Create a new component with specified parameters
* `delete_component` - Remove a component from the scene
* `transform_component` - Move, rotate, or scale a component
* `set_material` - Apply materials to components
* `export_scene` - Export the current scene to various formats
* `eval_ruby` - Execute arbitrary Ruby code in SketchUp for advanced operations
* `server_status` - What the SketchUp side is doing: the requests in flight, and the Ruby call stack of one that hasn't come back. Answered even while another request is running
* `lease_sketchup` / `release_sketchup` - Lease a SketchUp of the session's own from the pool (below), before any other tool, and release it when the task is done

### A pool of SketchUps

Several sessions on one Windows machine each drive a SketchUp of their own
(`src/sketchup_mcp/launcher.py`): a pool of four instances, on ports
9877-9880, each with its own copy of a template model
(`SKETCHUP_POOL_TEMPLATE`) and its own source folder (the lease's `src`,
passed to Shellify's dev loader as `SHELLIFY_SRC`). The server's
instructions tell every session these rules.

* `lease_sketchup(src, label)` leases one. `src` is required, with no
  default: the folder the session works in (its worktree, or the main
  checkout). A lease is refused at once if it's missing, isn't an existing
  folder, or has no `shellify.rb` in it. A lease prefers a free instance
  already running on `src`, and otherwise starts or restarts one on a fresh
  copy of the template. Its reply says which (`fresh`). Every tool goes to the
  leased SketchUp, and fails until there is one. A session holds one at a
  time; leasing again returns the same one.
* `release_sketchup()` frees it, as soon as the task is done; SketchUp keeps
  running. A lease also ends when its MCP server exits.
* If a leased SketchUp dies, the next call runs nothing, leases again on
  the `src` recorded with the lease, and says so.
* While leased, a pool instance shows Claude's icon in the bottom left of
  its viewport (an overlay; hovering shows the `label`), and a "Claude"
  toolbar whose button opens the session that holds it, when the server ran
  in the Claude desktop app (`su_mcp/su_mcp/lease_badge.rb`). The icon,
  `su_mcp/su_mcp/images/claude.svg`, is from Wikimedia Commons
  (File:Claude-ai-icon.svg, CC0); the PNGs beside it are renders of it.
* `sketchup-launcher status | lease --src <folder> [--label <text>] | release`
  does the same from a shell (`--src` is required, and checked the same
  way); the lease belongs to that shell, or to `--holder <pid>`.

The state is `%LOCALAPPDATA%\sketchup-mcp\pool.json`, under a lock file.
SketchUp is started by a scheduled task per instance ("SketchUp pool
sketchup-N"), not as a child of the MCP server: an MCP client may run its
server in a job object that kills its children when it closes, and the Claude
desktop app, a packaged app, virtualizes `%LOCALAPPDATA%` for everything it
starts, so files it creates there can be invisible to SketchUp. The module's
docstring has the details.

### Example Commands

Here are some examples of what you can ask Claude to do:

* "Create a simple house model with a roof and windows"
* "Select all components and get their information"
* "Make the selected component red"
* "Move the selected component 10 units up"
* "Export the current scene as a 3D model"
* "Create a complex arts and crafts cabinet using Ruby code"

## Troubleshooting

* **Connection issues**: Make sure both the Sketchup extension server and the MCP server are running
* **Command failures**: Check the Ruby Console in Sketchup for error messages
* **Timeout errors**: Try simplifying your requests or breaking them into smaller steps

## Technical Details

### Communication Protocol

The system uses a simple JSON-based protocol over TCP sockets:

* **Commands** are sent as JSON objects with a `type` and optional `params`
* **Responses** are JSON objects with a `status` and `result` or `message`

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT 