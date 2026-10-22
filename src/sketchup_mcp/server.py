from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List

from . import launcher

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SketchupMCPServer")

# How long eval_ruby waits for SketchUp before giving up.
#
# Keep this under the MCP client's own request timeout (60s in the reference
# SDKs). FastMCP runs these synchronous tools on its event loop, so while one
# call is waiting here the whole server is frozen. If the client gives up
# first, every call behind this one times out too, which reads as if SketchUp
# had stopped answering. Giving up here first returns a real error and frees
# the server for the next call.
EVAL_TIMEOUT = float(os.environ.get("SKETCHUP_MCP_EVAL_TIMEOUT", "50"))

# Define version directly to avoid pkg_resources dependency
__version__ = "0.2.0"
logger.info(f"SketchupMCP Server version {__version__} starting up")

class SketchupRubyError(Exception):
    """Sketchup handled the request and returned a JSON-RPC error.

    Distinct from a transport failure: the socket is still good, so it is
    not torn down on the way out.
    """


@dataclass
class SketchupConnection:
    host: str
    port: int
    sock: socket.socket = None
    # Bytes read off the socket that are not yet a complete JSON message.
    # Belongs to this socket and only this socket, so it is cleared whenever
    # the socket is.
    buffer: bytes = b""
    # One request at a time on this socket. Tools run in threads (see
    # eval_ruby), so two calls to one instance would otherwise interleave.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def connect(self) -> bool:
        """Connect to the Sketchup extension socket server

        An open socket is reused unless a peek shows the extension has hung
        up on it (see _peer_closed). Without that check, the first call after
        SketchUp restarts goes down the dead socket and fails with WinError
        10053, and only the call after it reconnects. That failure cannot
        simply be retried for eval_ruby, since it can't be told apart from
        one where the code did run, so it has to be caught before sending.
        """
        if self.sock:
            if not self._peer_closed():
                return True
            logger.info("Sketchup closed the connection; reconnecting")
            self.disconnect()

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            self.buffer = b""
            logger.info(f"Connected to Sketchup at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Sketchup: {str(e)}")
            self.sock = None
            return False
    
    def _peer_closed(self) -> bool:
        """Whether the cached socket is no use for a new request

        A non-blocking peek, which unlike a zero-byte send does see the
        peer's FIN (recv returns b"") or RST (recv raises). Any bytes waiting
        before we have sent anything also count: they are a reply nobody is
        waiting for, and reading them as the next response would put every
        later call one reply out of step.
        """
        if self.buffer:
            return True
        previous = self.sock.gettimeout()
        try:
            self.sock.setblocking(False)
            self.sock.recv(1, socket.MSG_PEEK)
            return True
        except BlockingIOError:
            return False
        except OSError:
            return True
        finally:
            try:
                self.sock.settimeout(previous)
            except OSError:
                pass

    def disconnect(self):
        """Disconnect from the Sketchup extension"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Sketchup: {str(e)}")
            finally:
                self.sock = None
                self.buffer = b""

    def receive_response(self, timeout=15.0):
        """Read until one complete JSON object is available and return it

        The extension writes one object per response and keeps the socket
        open, so anything after the object we want belongs to a later read.
        Scan the buffer for a single complete object, consume exactly that,
        and leave the rest alone - re-parsing the whole buffer after every
        chunk would both throw away the remainder and mis-handle a payload
        that arrives in pieces.
        """
        self.sock.settimeout(timeout)

        while True:
            parsed, consumed = self._take_json_prefix(self.buffer)
            if consumed:
                self.buffer = self.buffer[consumed:]
                if parsed is None:
                    raise Exception("Malformed JSON response from Sketchup")
                return parsed

            chunk = self.sock.recv(8192)
            if not chunk:
                if not self.buffer:
                    raise ConnectionError("Connection closed before receiving any data")
                raise ConnectionError("Connection closed mid-response")
            self.buffer += chunk

    @staticmethod
    def _take_json_prefix(buf: bytes):
        """Pull one complete JSON object off the front of *buf*

        Returns (object, bytes_consumed). (None, 0) means the buffer holds
        only part of a message; (None, n) means the first n bytes are a
        balanced but unparseable object, which the caller drops rather than
        wedging the stream on it.
        """
        i = 0
        while i < len(buf) and buf[i : i + 1].isspace():
            i += 1
        if i == len(buf):
            return None, 0

        depth = 0
        in_string = False
        escape = False
        j = i
        while j < len(buf):
            c = buf[j : j + 1]
            if in_string:
                if escape:
                    escape = False
                elif c == b"\\":
                    escape = True
                elif c == b'"':
                    in_string = False
            elif c == b'"':
                in_string = True
            elif c == b"{":
                depth += 1
            elif c == b"}":
                depth -= 1
                if depth <= 0:
                    # Take trailing whitespace too, so a quiet connection
                    # sits on an empty buffer.
                    end = j + 1
                    while end < len(buf) and buf[end : end + 1].isspace():
                        end += 1
                    try:
                        return json.loads(buf[i : j + 1].decode("utf-8")), end
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return None, end
            j += 1
        return None, 0

    def send_command(
        self,
        method: str,
        params: Dict[str, Any] = None,
        request_id: Any = None,
        timeout: float = 15.0,
        retries: int = 2
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request to Sketchup and return the response

        timeout/retries are per call: eval_ruby can run for a long time and
        is not safe to repeat, so it needs a long timeout and no automatic
        retry (a retry after a timeout runs the same code a second time).
        """
        with self.lock:
            return self._send_command(method, params, request_id, timeout, retries)

    def _send_command(self, method, params, request_id, timeout, retries):
        # Try to connect if not connected
        if not self.connect():
            raise ConnectionError(
                f"Could not connect to Sketchup at {self.host}:{self.port}. "
                "Its SketchUp may still be loading, or stopped at a dialog; if it "
                "has died, the next call leases again."
            )
        
        # Ensure we're sending a proper JSON-RPC request
        if method == "tools/call" and params and "name" in params and "arguments" in params:
            # This is already in the correct format
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": request_id
            }
        else:
            # This is a direct command - convert to JSON-RPC
            command_name = method
            command_params = params or {}
            
            # Log the conversion
            logger.info(f"Converting direct command '{command_name}' to JSON-RPC format")
            
            request = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": command_name,
                    "arguments": command_params
                },
                "id": request_id
            }
        
        # Maximum number of retries
        max_retries = retries
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                logger.info(f"Sending JSON-RPC request: {request}")
                
                # Log the exact bytes being sent
                request_bytes = json.dumps(request).encode('utf-8') + b'\n'
                logger.info(f"Raw bytes being sent: {request_bytes}")
                
                self.sock.sendall(request_bytes)
                logger.info(f"Request sent, waiting up to {timeout}s for response...")
                
                self.sock.settimeout(timeout)
                
                response = self.receive_response(timeout=timeout)
                logger.info(f"Response parsed: {response}")

                if not isinstance(response, dict):
                    return response

                if "error" in response:
                    logger.error(f"Sketchup error: {response['error']}")
                    raise SketchupRubyError(
                        response["error"].get("message", "Unknown error from Sketchup")
                    )

                return response.get("result", {})
                
            except (socket.timeout, ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                logger.warning(f"Connection error (attempt {retry_count+1}/{max_retries+1}): {str(e)}")
                retry_count += 1
                
                if retry_count <= max_retries:
                    logger.info(f"Retrying connection...")
                    self.disconnect()
                    if not self.connect():
                        logger.error("Failed to reconnect")
                        break
                else:
                    logger.error(f"Max retries reached, giving up")
                    self.disconnect()
                    if isinstance(e, socket.timeout):
                        raise Exception(
                            f"No response from Sketchup within {timeout:g}s. The "
                            "request may still be running there; it was not "
                            "sent again. Check the Ruby Console for the last "
                            "'Request #N started' without a 'finished'."
                        )
                    raise Exception(f"Connection to Sketchup lost after {max_retries+1} attempts: {str(e)}")
            
            except SketchupRubyError:
                # Sketchup answered, it just answered with an error. The
                # connection is fine; keep it.
                raise

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON response from Sketchup: {str(e)}")
                raise Exception(f"Invalid response from Sketchup: {str(e)}")

            except Exception as e:
                logger.error(f"Error communicating with Sketchup: {str(e)}")
                self.disconnect()
                raise Exception(f"Communication error with Sketchup: {str(e)}")

# One connection per SketchUp, by (host, port). A session has one lease at a
# time, but keeps the connection to an instance it leased before.
_connections: Dict[tuple, SketchupConnection] = {}
_connections_lock = threading.Lock()
_holder = None

def session_holder():
    """This MCP server's process, which its lease belongs to (see launcher)."""
    global _holder
    if _holder is None:
        _holder = launcher.holder()
    return _holder

def session_identity():
    """(id, link) of the Claude desktop app's session that started this
    server, or (None, None). Read from CLAUDE_CODE_HOST_SESSION_ID, which
    Claude Code sets for what it starts in the desktop app; the link is the
    form the app gives its sessions (claude://claude.ai/epitaxy/<id>). Both
    observed in Claude 3.1623 (2026-10-22), not documented."""
    session = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or None
    link = f"claude://claude.ai/epitaxy/{session}" if session else None
    return session, link

def resolve_instance():
    """Where this session's tool calls go: its leased SketchUp's (host, port).

    There's no default: with no lease, the call fails and says to lease. If
    the leased SketchUp has died, leases again and raises, so nothing is run
    on a model the caller didn't expect.
    """
    record = launcher.held(session_holder())
    if record is None:
        raise Exception(
            "No SketchUp leased. Call lease_sketchup first (with src = your working folder), "
            "and release_sketchup when your task is done."
        )
    if not record["alive"]:
        src = (record["env"] or {}).get("SHELLIFY_SRC")
        lease = record["lease"] or {}
        try:
            new = launcher.lease(src, session_holder(), label=lease.get("label"),
                                 session=lease.get("session"), link=lease.get("link"))
        except Exception as e:
            raise Exception(
                f"{record['name']}'s SketchUp had died, so nothing was run, and leasing "
                f"again failed: {e}"
            )
        raise Exception(
            f"{record['name']}'s SketchUp had died, so nothing was run, and what was built "
            f"or loaded in it is gone. Instead: {new['message']} Resend once it's set up again."
        )
    return launcher.HOST, record["port"]

def get_sketchup_connection() -> SketchupConnection:
    """The connection to this session's leased SketchUp (see resolve_instance)

    No health check here. The ping this used to send was never read back,
    so its reply would sit in the stream ahead of the next real response -
    and a send on a socket whose peer has already gone succeeds locally
    anyway, so it never detected the thing it was meant to detect. A dead
    socket now surfaces as a failed send or read in send_command, which
    disconnects; its next call reconnects. Connecting happens there too,
    under the connection's lock, since a request may be running on it in
    another thread.
    """
    key = resolve_instance()
    with _connections_lock:
        if key not in _connections:
            _connections[key] = SketchupConnection(host=key[0], port=key[1])
        return _connections[key]

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("SketchupMCP server starting up")
        yield {}
    finally:
        for connection in list(_connections.values()):
            connection.disconnect()
        _connections.clear()
        try:
            # They'd be freed anyway once this process has gone.
            launcher.release(session_holder())
        except Exception as e:
            logger.warning(f"Could not release this session's lease: {e}")
        logger.info("SketchupMCP server shut down")

# Create MCP server with lifespan support
mcp = FastMCP(
    "SketchupMCP",
    instructions=(
        "SketchUp, driven through eval_ruby. Every session uses a SketchUp of its own from a "
        "pool, one at a time: call lease_sketchup before any other tool. Its src is required: "
        "the folder you work in, which SketchUp loads Shellify from (your worktree, or "
        "~/source/shellify if you work in the main checkout); it must have shellify.rb in it, "
        "and there's no default. Set label to your session's title or a few words on your task. "
        "Tools fail until you have a lease; there is no default SketchUp. "
        "While leased, the SketchUp shows a Claude badge, so the user knows not to touch it. "
        "Call release_sketchup as soon as your task is done, before you end your turn, even if "
        "the user may come back later: an idle lease keeps a SketchUp from everyone else. If "
        "you come back to work after releasing, lease again; you may get another SketchUp, "
        "or a fresh model, so check what you rely on."
    ),
    lifespan=server_lifespan
)

# Tool endpoints
@mcp.tool()
def create_component(
    ctx: Context,
    type: str = "cube",
    position: List[float] = None,
    dimensions: List[float] = None
) -> str:
    """Create a new component in Sketchup"""
    try:
        logger.info(f"create_component called with type={type}, position={position}, dimensions={dimensions}, request_id={ctx.request_id}")
        
        sketchup = get_sketchup_connection()
        
        params = {
            "name": "create_component",
            "arguments": {
                "type": type,
                "position": position or [0,0,0],
                "dimensions": dimensions or [1,1,1]
            }
        }
        
        logger.info(f"Calling send_command with method='tools/call', params={params}, request_id={ctx.request_id}")
        
        result = sketchup.send_command(
            method="tools/call",
            params=params,
            request_id=ctx.request_id
        )
        
        logger.info(f"create_component result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_component: {str(e)}")
        return f"Error creating component: {str(e)}"

@mcp.tool()
def delete_component(
    ctx: Context,
    id: str
) -> str:
    """Delete a component by ID"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "delete_component",
                "arguments": {"id": id}
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error deleting component: {str(e)}"

@mcp.tool()
def transform_component(
    ctx: Context,
    id: str,
    position: List[float] = None,
    rotation: List[float] = None,
    scale: List[float] = None
) -> str:
    """Transform a component's position, rotation, or scale"""
    try:
        sketchup = get_sketchup_connection()
        arguments = {"id": id}
        if position is not None:
            arguments["position"] = position
        if rotation is not None:
            arguments["rotation"] = rotation
        if scale is not None:
            arguments["scale"] = scale
            
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "transform_component",
                "arguments": arguments
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error transforming component: {str(e)}"

@mcp.tool()
def get_selection(ctx: Context) -> str:
    """Get currently selected components"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "get_selection",
                "arguments": {}
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error getting selection: {str(e)}"

@mcp.tool()
def set_material(
    ctx: Context,
    id: str,
    material: str
) -> str:
    """Set material for a component"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "set_material",
                "arguments": {
                    "id": id,
                    "material": material
                }
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error setting material: {str(e)}"

@mcp.tool()
def export_scene(
    ctx: Context,
    format: str = "skp"
) -> str:
    """Export the current scene"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "export",
                "arguments": {
                    "format": format
                }
            },
            request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error exporting scene: {str(e)}"

@mcp.tool()
def create_mortise_tenon(
    ctx: Context,
    mortise_id: str,
    tenon_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0
) -> str:
    """Create a mortise and tenon joint between two components"""
    try:
        logger.info(f"create_mortise_tenon called with mortise_id={mortise_id}, tenon_id={tenon_id}, width={width}, height={height}, depth={depth}, offsets=({offset_x}, {offset_y}, {offset_z})")
        
        sketchup = get_sketchup_connection()
        
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "create_mortise_tenon",
                "arguments": {
                    "mortise_id": mortise_id,
                    "tenon_id": tenon_id,
                    "width": width,
                    "height": height,
                    "depth": depth,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "offset_z": offset_z
                }
            },
            request_id=ctx.request_id
        )
        
        logger.info(f"create_mortise_tenon result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_mortise_tenon: {str(e)}")
        return f"Error creating mortise and tenon joint: {str(e)}"

@mcp.tool()
def create_dovetail(
    ctx: Context,
    tail_id: str,
    pin_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    angle: float = 15.0,
    num_tails: int = 3,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0
) -> str:
    """Create a dovetail joint between two components"""
    try:
        logger.info(f"create_dovetail called with tail_id={tail_id}, pin_id={pin_id}, width={width}, height={height}, depth={depth}, angle={angle}, num_tails={num_tails}")
        
        sketchup = get_sketchup_connection()
        
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "create_dovetail",
                "arguments": {
                    "tail_id": tail_id,
                    "pin_id": pin_id,
                    "width": width,
                    "height": height,
                    "depth": depth,
                    "angle": angle,
                    "num_tails": num_tails,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "offset_z": offset_z
                }
            },
            request_id=ctx.request_id
        )
        
        logger.info(f"create_dovetail result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_dovetail: {str(e)}")
        return f"Error creating dovetail joint: {str(e)}"

@mcp.tool()
def create_finger_joint(
    ctx: Context,
    board1_id: str,
    board2_id: str,
    width: float = 1.0,
    height: float = 1.0,
    depth: float = 1.0,
    num_fingers: int = 5,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    offset_z: float = 0.0
) -> str:
    """Create a finger joint (box joint) between two components"""
    try:
        logger.info(f"create_finger_joint called with board1_id={board1_id}, board2_id={board2_id}, width={width}, height={height}, depth={depth}, num_fingers={num_fingers}")
        
        sketchup = get_sketchup_connection()
        
        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "create_finger_joint",
                "arguments": {
                    "board1_id": board1_id,
                    "board2_id": board2_id,
                    "width": width,
                    "height": height,
                    "depth": depth,
                    "num_fingers": num_fingers,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "offset_z": offset_z
                }
            },
            request_id=ctx.request_id
        )
        
        logger.info(f"create_finger_joint result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_finger_joint: {str(e)}")
        return f"Error creating finger joint: {str(e)}"

def _eval_ruby(code: str, request_id) -> str:
    try:
        logger.info(f"eval_ruby called with code length: {len(code)}")

        sketchup = get_sketchup_connection()

        result = sketchup.send_command(
            method="tools/call",
            params={
                "name": "eval_ruby",
                "arguments": {
                    "code": code
                }
            },
            request_id=request_id,
            # Ruby is not idempotent: a timeout does not mean it failed, so
            # retrying would run it again. Never retry, and see EVAL_TIMEOUT
            # for why the wait is bounded by the client's timeout.
            timeout=EVAL_TIMEOUT,
            retries=0
        )
        
        logger.info(f"eval_ruby result: {result}")
        
        # Format the response to include the result
        response = {
            "success": True,
            "result": result.get("content", [{"text": "Success"}])[0].get("text", "Success") if isinstance(result.get("content"), list) and len(result.get("content", [])) > 0 else "Success"
        }
        
        return json.dumps(response)
    except Exception as e:
        logger.error(f"Error in eval_ruby: {str(e)}")
        return json.dumps({
            "success": False,
            "error": str(e)
        })

@mcp.tool()
async def eval_ruby(
    ctx: Context,
    code: str
) -> str:
    """Evaluate arbitrary Ruby code in this session's leased SketchUp (see
    lease_sketchup)."""
    # In a thread, so the server keeps answering meanwhile, and server_status
    # still gets through.
    return await asyncio.to_thread(_eval_ruby, code, ctx.request_id)

def _server_status(request_id) -> str:
    try:
        host, port = resolve_instance()
        # A connection of its own, so it isn't queued behind a request
        # running on the instance's usual one.
        sketchup = SketchupConnection(host=host, port=port)
        try:
            result = sketchup.send_command(
                method="tools/call",
                params={"name": "server_status", "arguments": {}},
                request_id=request_id,
                timeout=10.0,
                retries=0
            )
        finally:
            sketchup.disconnect()
        content = result.get("content") if isinstance(result, dict) else None
        text = content[0].get("text") if isinstance(content, list) and content else json.dumps(result)
        return json.dumps({"success": True, "result": text})
    except Exception as e:
        logger.error(f"Error in server_status: {str(e)}")
        return json.dumps({"success": False, "error": str(e)})

@mcp.tool()
async def server_status(ctx: Context) -> str:
    """Say what the SketchUp side of the server is doing: the requests in
    flight and for how long, and the Ruby call stack, which shows where a
    request that hasn't come back is. Runs no code in the model, so it's
    answered even while another request is running (other tool calls then
    get a 'busy' error)."""
    return await asyncio.to_thread(_server_status, ctx.request_id)

def _lease_reply(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "success": True,
        "name": record["name"],
        "port": record["port"],
        "pid": record["pid"],
        "model": record["model"],
        "src": (record["env"] or {}).get("SHELLIFY_SRC"),
        "fresh": record["fresh"],
        "message": record["message"],
    }

@mcp.tool()
async def lease_sketchup(
    ctx: Context,
    src: str,
    label: str = None
) -> str:
    """Lease a SketchUp of this session's own from the pool. Call it before
    any other SketchUp tool: with no lease they fail. A session holds one
    SketchUp at a time; leasing again returns the same one.

    *src* is required: the folder you work in, which SketchUp loads
    Shellify from (your worktree, or ~/source/shellify if you work in the
    main checkout). There's no default, and a folder that doesn't exist or
    has no shellify.rb is refused. *label* is shown in the
    SketchUp's viewport badge: your session's title, or a few words on your
    task. A free instance already running on *src* is preferred; otherwise
    one is started, or restarted, on a fresh copy of the template model,
    which can take a minute. The reply's "fresh" says which: a reused
    instance's model is as its last holder left it.

    Release it with release_sketchup as soon as your task is done. If the
    leased SketchUp dies, the next call leases again and says so."""
    try:
        session, link = session_identity()
        record = await asyncio.to_thread(
            launcher.lease, src, session_holder(), label, session, link)
        return json.dumps(_lease_reply(record))
    except Exception as e:
        logger.error(f"Error in lease_sketchup: {str(e)}")
        return json.dumps({"success": False, "error": str(e)})

@mcp.tool()
async def release_sketchup(ctx: Context) -> str:
    """Release this session's SketchUp, as soon as your task is done, even if
    the user may come back later. SketchUp keeps running, for the next
    lease; lease again to go on working."""
    try:
        record = await asyncio.to_thread(launcher.release, session_holder())
        if record is None:
            return json.dumps({"success": False, "error": "This session holds no lease"})
        return json.dumps({
            "success": True,
            "message": f"Released {record['name']} (port {record['port']}); it keeps running.",
        })
    except Exception as e:
        logger.error(f"Error in release_sketchup: {str(e)}")
        return json.dumps({"success": False, "error": str(e)})

def main():
    mcp.run()

if __name__ == "__main__":
    main()