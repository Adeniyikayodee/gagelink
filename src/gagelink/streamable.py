"""The MCP Streamable HTTP transport, for clients that cannot spawn a process.

Stdio is the right default: it needs no port, no origin check, and no session table, and
`uvx --from gagelink gagelink-mcp` runs the whole thing with nothing installed. It also
requires the client to be on the same machine as the server, which rules out every web
client and every hosted connector. Since the thing this package most needs is one person
outside this work running it against a question of their own, the transport that lets them
do it from a URL is worth the code.

Two things are done here that stdio does not have to do, and both are security rather than
protocol. The origin of every request is checked, because a page in a browser can otherwise
reach a server bound to the loopback address and drive it; that is DNS rebinding, and the
specification requires the check. And the server binds to the loopback address unless told
otherwise, so that starting it does not put a hydrology client on a network by accident.

Each session gets its own toolkit, which stdio cannot offer: one process serves several
conversations at once without their manifests or their quantities meeting.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .server import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, MethodNotFound, Server, dispatch

#: Where the endpoint lives. One path serves every method, which is what makes the
#: transport streamable rather than a pair of endpoints.
DEFAULT_PATH = "/mcp"

#: The loopback address. Chosen as the default bind so that running the server exposes it
#: to this machine and to nothing else.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Hosts a browser page may be served from and still reach this server. Anything else is
#: refused before the body is read.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

#: A ceiling on one request body. A tool call is a few hundred bytes and nothing here has
#: any reason to accept a megabyte.
MAX_BODY_BYTES = 1 << 20


class Conversation:
    """One session, and the single thread that owns it from open to close.

    This exists because of a collision between two reasonable designs. A session is a
    context manager, and the provenance ledger it enters is held in a `ContextVar`, whose
    token may only be reset in the context that created it. A threading HTTP server hands
    each request to a fresh thread, so a session opened while answering `initialize` can
    never be closed while answering anything else: the close raises, and the ledger for
    that conversation is left open.

    Serving on one thread would fix it and would also serialise every caller behind
    whichever tool call is waiting on a service, which is most of them. So the session
    gets a thread of its own instead. Everything that touches it, including its creation
    and its close, happens there, and requests are handed across a queue. Conversations
    still run concurrently with each other, which is the property worth keeping.
    """

    def __init__(self, factory: Callable[[], Server], timeout: float = 120.0) -> None:
        self._work: queue.Queue[tuple[str, dict[str, Any], queue.Queue[Any]] | None] = queue.Queue()
        self._timeout = timeout
        self._started = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(factory,), daemon=True)
        self._thread.start()
        self._started.wait(timeout)
        if self._failure is not None:
            raise self._failure

    def _run(self, factory: Callable[[], Server]) -> None:
        try:
            server = factory()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller below
            self._failure = exc
            self._started.set()
            return
        self._started.set()
        try:
            while True:
                item = self._work.get()
                if item is None:
                    return
                method, params, reply = item
                try:
                    reply.put(("ok", dispatch(server, method, params)))
                except BaseException as exc:  # noqa: BLE001 - travels back to the request
                    reply.put(("raised", exc))
        finally:
            if server.session is not None:
                server.session.__exit__(None, None, None)

    def ask(self, method: str, params: dict[str, Any]) -> Any:
        """Run one JSON-RPC method on this conversation's thread and return its result."""
        reply: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._work.put((method, params, reply))
        try:
            outcome, value = reply.get(timeout=self._timeout)
        except queue.Empty:
            raise TimeoutError(f"{method} did not finish within {self._timeout:g} seconds") from None
        if outcome == "raised":
            raise value
        return value

    def close(self) -> None:
        self._work.put(None)
        self._thread.join(timeout=self._timeout)


class Sessions:
    """The conversations this endpoint is holding, each with its own toolkit.

    A session is created by `initialize`, addressed afterwards by the identifier this
    hands back, and dropped on DELETE or when the endpoint shuts down. Keeping them apart
    is what lets one process answer two people without either one's manifest picking up
    the other's retrievals, which the stdio transport cannot offer at all.
    """

    def __init__(self, factory: Callable[[], Server], limit: int = 64) -> None:
        self._factory = factory
        self._limit = limit
        self._sessions: dict[str, Conversation] = {}
        self._lock = threading.Lock()

    def open(self) -> tuple[str, Conversation]:
        conversation = Conversation(self._factory)
        identifier = secrets.token_hex(16)
        with self._lock:
            if len(self._sessions) >= self._limit:
                # Oldest first, which in a dict is insertion order. A bound is needed
                # because a session is only closed by a well-behaved client, and a server
                # that keeps every conversation it was ever sent runs out of memory.
                oldest = next(iter(self._sessions))
                self._sessions.pop(oldest).close()
            self._sessions[identifier] = conversation
        return identifier, conversation

    def get(self, identifier: str | None) -> Conversation | None:
        with self._lock:
            return self._sessions.get(identifier or "")

    def close(self, identifier: str) -> bool:
        with self._lock:
            conversation = self._sessions.pop(identifier, None)
        if conversation is None:
            return False
        conversation.close()
        return True

    def close_all(self) -> None:
        with self._lock:
            held, self._sessions = list(self._sessions.values()), {}
        for conversation in held:
            conversation.close()

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


def origin_is_allowed(origin: str | None, allowed: frozenset[str]) -> bool:
    """Whether a request carrying this `Origin` may be served.

    An absent origin is allowed, because a command-line client or another server sends
    none and refusing those would refuse every non-browser caller. A present origin has to
    name a host on the list, since that is the case the check exists for: a page the user
    did not write, running in their browser, reaching a server on their own machine.
    """
    if origin is None:
        return True
    if "*" in allowed:
        return True
    host = urlparse(origin).hostname
    return host is not None and host.lower() in allowed


class Handler(BaseHTTPRequestHandler):
    """One endpoint, answering POST, GET, and DELETE as the transport describes."""

    protocol_version = "HTTP/1.1"
    server_version = "gagelink"
    sys_version = ""

    # Set on the class by serve_http.
    sessions: Sessions
    path_served: str = DEFAULT_PATH
    allowed_origins: frozenset[str] = frozenset(LOCAL_HOSTS)
    quiet: bool = True

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence the default access log, which writes to stderr and would interleave
        with anything else the process is saying."""
        if not self.quiet:
            super().log_message(format, *args)

    # Answering ---------------------------------------------------------------------------

    def _send(self, status: int, payload: Any = None, headers: dict[str, str] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, message: str, request_id: Any = None) -> None:
        """A transport failure, which is not the same thing as a tool failure.

        A tool that cannot answer returns a result saying so and the conversation
        continues. This is for the cases before a tool is reached: a wrong path, an
        unknown session, an origin that may not ask.
        """
        code = -32600 if status < 500 else -32603
        self._send(status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _guard(self) -> bool:
        """The checks every method shares. False means an answer has already been sent."""
        if urlparse(self.path).path != self.path_served:
            self._error(404, f"nothing is served from {self.path}; the endpoint is {self.path_served}")
            return False
        if not origin_is_allowed(self.headers.get("Origin"), self.allowed_origins):
            self._error(403, "this origin may not reach the server")
            return False
        version = self.headers.get("MCP-Protocol-Version")
        if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
            self._error(400, f"unsupported protocol version {version!r}")
            return False
        return True

    # Methods -----------------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._error(413, "the request body is larger than this server accepts")
            return
        try:
            message = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError as exc:
            self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
            return
        if not isinstance(message, dict):
            self._error(400, "a request is one JSON-RPC message; batching was withdrawn from the protocol")
            return

        method, request_id = message.get("method"), message.get("id")
        given = self.headers.get("Mcp-Session-Id")

        if method == "initialize":
            identifier, conversation = self.sessions.open()
        else:
            found = self.sessions.get(given)
            if found is None:
                # 404 rather than 400 is what tells a client to start again by
                # initialising, which is the recovery the specification describes.
                self._error(404, "no such session; initialize to start one", request_id)
                return
            identifier, conversation = given or "", found

        if request_id is None:
            # A notification or a response. There is nothing to answer and saying so with
            # 202 is what distinguishes it from a request whose result went missing.
            self._send(202)
            return

        try:
            result = conversation.ask(method or "", message.get("params") or {})
        except MethodNotFound as exc:
            reply: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {exc}"},
            }
        except Exception as exc:
            reply = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}
        else:
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}

        headers = {"Mcp-Session-Id": identifier} if method == "initialize" else {}
        self._send(200, reply, headers)

    def do_GET(self) -> None:  # noqa: N802
        """No server-initiated stream is offered, and saying so is better than opening one.

        The transport allows a client to open an event stream here for messages the server
        starts. Every answer this server has is a reply to a request, so an open stream
        would hold a connection and never carry anything.
        """
        if not self._guard():
            return
        self._send(405, None, {"Allow": "POST, DELETE"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._guard():
            return
        given = self.headers.get("Mcp-Session-Id")
        if given is None or not self.sessions.close(given):
            self._error(404, "no such session")
            return
        self._send(204)


def serve_http(
    factory: Callable[[], Server],
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    allowed_origins: frozenset[str] | None = None,
    quiet: bool = True,
) -> ThreadingHTTPServer:
    """Start the endpoint and return it, without blocking.

    Returned rather than served in place so that a test can start one, ask it something,
    and shut it down, which is the only way to check a transport without mocking the thing
    under test.
    """
    sessions = Sessions(factory)

    class Bound(Handler):
        pass

    Bound.sessions = sessions
    Bound.path_served = path
    Bound.allowed_origins = allowed_origins or frozenset(LOCAL_HOSTS)
    Bound.quiet = quiet

    httpd = ThreadingHTTPServer((host, port), Bound)
    httpd.daemon_threads = True
    setattr(httpd, "sessions", sessions)
    return httpd


__all__ = [
    "Conversation",
    "DEFAULT_HOST",
    "DEFAULT_PATH",
    "DEFAULT_PORT",
    "Handler",
    "Sessions",
    "origin_is_allowed",
    "serve_http",
]
