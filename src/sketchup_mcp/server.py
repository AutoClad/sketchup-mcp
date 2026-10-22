import json
import logging
import os
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SketchupMCPServer")

# Define version directly to avoid pkg_resources dependency
__version__ = "0.1.17"
logger.info(f"SketchupMCP Server version {__version__} starting up")


def _new_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


@dataclass
class SketchupConnection:
    """Talks to the SketchUp extension's socket server.

    The extension serves ONE request per connection, on SketchUp's main
    thread: it accepts, reads one newline-terminated JSON-RPC request, writes
    one reply, and closes. So this client opens a fresh connection for each
    command and closes it as soon as the reply is read. It never holds an
    idle connection: an open connection with no request on it is exactly what
    the extension is waiting on, and while it waits SketchUp is unresponsive.
    """

    host: str
    port: int
    socket_factory: Callable[[], socket.socket] = field(default=_new_socket)
    timeout: float = 15.0

    def send_command(self, method: str, params: dict[str, Any] = None, request_id: Any = None) -> dict[str, Any]:
        """Send a JSON-RPC request to Sketchup and return the response"""
        if method == "tools/call" and params and "name" in params and "arguments" in params:
            request = {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
        else:
            # A direct command: wrap it as a tools/call.
            request = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": method, "arguments": params or {}},
                "id": request_id,
            }
        request_bytes = json.dumps(request).encode("utf-8") + b"\n"

        # Only failures to connect are retried: once the request is sent,
        # retrying could run a command (say, an eval) twice.
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            sock = self.socket_factory()
            try:
                sock.settimeout(self.timeout)
                try:
                    sock.connect((self.host, self.port))
                except (ConnectionRefusedError, TimeoutError, OSError) as e:
                    if attempt == max_attempts:
                        raise Exception(
                            f"Could not connect to Sketchup at {self.host}:{self.port}: {e}. "
                            "In SketchUp: Extensions > MCP Server > Start Server."
                        ) from e
                    logger.warning(f"Connect attempt {attempt}/{max_attempts} failed: {e}")
                    continue

                logger.info(f"Sending JSON-RPC request: {request}")
                sock.sendall(request_bytes)
                response_data = self.receive_full_response(sock)
            finally:
                sock.close()

            try:
                response = json.loads(response_data.decode("utf-8"))
            except json.JSONDecodeError as e:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
                raise Exception(f"Invalid response from Sketchup: {str(e)}") from e

            if not isinstance(response, dict):
                return response
            if "error" in response:
                logger.error(f"Sketchup error: {response['error']}")
                raise Exception(response["error"].get("message", "Unknown error from Sketchup"))
            return response.get("result", {})

        raise Exception("unreachable")

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        try:
            while True:
                chunk = sock.recv(buffer_size)
                if not chunk:
                    if not chunks:
                        raise Exception("Connection closed before receiving any data")
                    break
                chunks.append(chunk)
                try:
                    data = b"".join(chunks)
                    json.loads(data.decode("utf-8"))
                    logger.info(f"Received complete response ({len(data)} bytes)")
                    return data
                except json.JSONDecodeError:
                    continue
        except TimeoutError:
            logger.warning("Socket timeout during receive")

        if chunks:
            data = b"".join(chunks)
            try:
                json.loads(data.decode("utf-8"))
                return data
            except json.JSONDecodeError as e:
                raise Exception("Incomplete JSON response received") from e
        raise Exception("No data received")


def get_sketchup_connection() -> SketchupConnection:
    """A client for the configured SketchUp. Opens no socket: each command does."""
    return SketchupConnection(
        host=os.environ.get("SKETCHUP_HOST", "localhost"), port=int(os.environ.get("SKETCHUP_PORT", "9876"))
    )


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Startup and shutdown. Deliberately does not connect to SketchUp: an idle
    connection would block the extension, and SketchUp with it, until the
    first tool call (see SketchupConnection)."""
    logger.info("SketchupMCP server starting up")
    try:
        yield {}
    finally:
        logger.info("SketchupMCP server shut down")


# Create MCP server with lifespan support
mcp = FastMCP(
    "SketchupMCP", instructions="Sketchup integration through the Model Context Protocol", lifespan=server_lifespan
)


# Tool endpoints
@mcp.tool()
def create_component(
    ctx: Context, type: str = "cube", position: list[float] = None, dimensions: list[float] = None
) -> str:
    """Create a new component in Sketchup"""
    try:
        logger.info(
            f"create_component called with type={type}, position={position}, "
            f"dimensions={dimensions}, request_id={ctx.request_id}"
        )

        sketchup = get_sketchup_connection()

        params = {
            "name": "create_component",
            "arguments": {"type": type, "position": position or [0, 0, 0], "dimensions": dimensions or [1, 1, 1]},
        }

        logger.info(f"Calling send_command with method='tools/call', params={params}, request_id={ctx.request_id}")

        result = sketchup.send_command(method="tools/call", params=params, request_id=ctx.request_id)

        logger.info(f"create_component result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_component: {str(e)}")
        return f"Error creating component: {str(e)}"


@mcp.tool()
def delete_component(ctx: Context, id: str) -> str:
    """Delete a component by ID"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call", params={"name": "delete_component", "arguments": {"id": id}}, request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error deleting component: {str(e)}"


@mcp.tool()
def transform_component(
    ctx: Context, id: str, position: list[float] = None, rotation: list[float] = None, scale: list[float] = None
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
            params={"name": "transform_component", "arguments": arguments},
            request_id=ctx.request_id,
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
            method="tools/call", params={"name": "get_selection", "arguments": {}}, request_id=ctx.request_id
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error getting selection: {str(e)}"


@mcp.tool()
def set_material(ctx: Context, id: str, material: str) -> str:
    """Set material for a component"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call",
            params={"name": "set_material", "arguments": {"id": id, "material": material}},
            request_id=ctx.request_id,
        )
        return json.dumps(result)
    except Exception as e:
        return f"Error setting material: {str(e)}"


@mcp.tool()
def export_scene(ctx: Context, format: str = "skp") -> str:
    """Export the current scene"""
    try:
        sketchup = get_sketchup_connection()
        result = sketchup.send_command(
            method="tools/call", params={"name": "export", "arguments": {"format": format}}, request_id=ctx.request_id
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
    offset_z: float = 0.0,
) -> str:
    """Create a mortise and tenon joint between two components"""
    try:
        logger.info(
            f"create_mortise_tenon called with mortise_id={mortise_id}, tenon_id={tenon_id}, "
            f"width={width}, height={height}, depth={depth}, offsets=({offset_x}, {offset_y}, {offset_z})"
        )

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
                    "offset_z": offset_z,
                },
            },
            request_id=ctx.request_id,
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
    offset_z: float = 0.0,
) -> str:
    """Create a dovetail joint between two components"""
    try:
        logger.info(
            f"create_dovetail called with tail_id={tail_id}, pin_id={pin_id}, width={width}, "
            f"height={height}, depth={depth}, angle={angle}, num_tails={num_tails}"
        )

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
                    "offset_z": offset_z,
                },
            },
            request_id=ctx.request_id,
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
    offset_z: float = 0.0,
) -> str:
    """Create a finger joint (box joint) between two components"""
    try:
        logger.info(
            f"create_finger_joint called with board1_id={board1_id}, board2_id={board2_id}, "
            f"width={width}, height={height}, depth={depth}, num_fingers={num_fingers}"
        )

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
                    "offset_z": offset_z,
                },
            },
            request_id=ctx.request_id,
        )

        logger.info(f"create_finger_joint result: {result}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error in create_finger_joint: {str(e)}")
        return f"Error creating finger joint: {str(e)}"


@mcp.tool()
def eval_ruby(ctx: Context, code: str) -> str:
    """Evaluate arbitrary Ruby code in Sketchup"""
    try:
        logger.info(f"eval_ruby called with code length: {len(code)}")

        sketchup = get_sketchup_connection()

        result = sketchup.send_command(
            method="tools/call", params={"name": "eval_ruby", "arguments": {"code": code}}, request_id=ctx.request_id
        )

        logger.info(f"eval_ruby result: {result}")

        # Format the response to include the result
        response = {
            "success": True,
            "result": result.get("content", [{"text": "Success"}])[0].get("text", "Success")
            if isinstance(result.get("content"), list) and len(result.get("content", [])) > 0
            else "Success",
        }

        return json.dumps(response)
    except Exception as e:
        logger.error(f"Error in eval_ruby: {str(e)}")
        return json.dumps({"success": False, "error": str(e)})


def main():
    mcp.run()


if __name__ == "__main__":
    main()
