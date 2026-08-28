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

The 2026-07-28 revision changed this transport rather than only the messages on it. It
removed protocol sessions, so there is no `Mcp-Session-Id` to mint and no GET stream to
open, and it added headers that mirror fields out of the body so that a load balancer can
route without parsing it. Those headers are checked here against the body they were copied
from, which the specification requires and which is worth doing for its own sake: a request
where the two disagree is one where whatever routed it and whatever answers it were working
from different instructions, and picking either is a guess about which.

Both shapes are served. A request carrying per-request metadata is answered statelessly;
one that opened with `initialize` keeps its session until it is deleted.
"""

from __future__ import annotations

import base64
import binascii
import json
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from .protocol import (
    LEGACY_PROTOCOL_VERSIONS,
    Envelope,
    HeaderMismatch,
    ProtocolError,
    UnsupportedProtocolVersion,
    named_by,
    read_envelope,
)
from .server import Server, dispatch

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

#: How much of an over-long body will be read and thrown away so that the refusal reaches
#: the client. None of it needs to be read to know the request is too big, but a client is
#: still writing while the server decides, and a socket closed under a half-written body
#: gives it a broken pipe rather than the 413 that says why. Draining lets an honest mistake
#: hear the answer; the cap is what stops the courtesy becoming the denial of service the
#: ceiling above exists to prevent.
MAX_DRAIN_BYTES = 8 << 20


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
            # Every ledger, not only the default one: a modern client naming a conversation
            # opens one beside it, and closing the thread without closing that leaves the
            # provenance context it entered open for the life of the process.
            server.close()

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

    def obtain(self, key: str) -> Conversation:
        """The conversation under a name the client chose, opening one if it is new.

        The modern revision mints no session identifiers, so a conversation that spans
        requests has to be named by the client instead. A name that has not been seen opens
        a conversation rather than failing, because there is no handshake left in which it
        could have been opened earlier.
        """
        with self._lock:
            found = self._sessions.get(key)
            if found is not None:
                return found
        conversation = Conversation(self._factory)
        with self._lock:
            # Checked again under the lock: two requests naming the same new conversation
            # can arrive at once, and the loser's thread is closed rather than orphaned.
            found = self._sessions.get(key)
            if found is not None:
                conversation.close()
                return found
            if len(self._sessions) >= self._limit:
                oldest = next(iter(self._sessions))
                self._sessions.pop(oldest).close()
            self._sessions[key] = conversation
            return conversation

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


#: What a client wraps a header value in when it cannot be written as plain ASCII. Both
#: markers are case-sensitive and have to appear exactly, so that a value which happens to
#: look like one can be distinguished from one that is.
BASE64_PREFIX = "=?base64?"
BASE64_SUFFIX = "?="

#: Where a modern request is filed when the client names no conversation. The null byte
#: keeps it out of reach of the hexadecimal identifiers a handshake session is given, so
#: the two kinds of key share a table without being able to collide.
UNNAMED_CONVERSATION = "\x00unnamed"


def decoded_header(name: str, raw: str) -> str:
    """A mirrored header value, decoded if the client marked it as Base64.

    A name or a URI is only advised to stay inside the characters a header may carry, so
    the transport defines an encoding for the ones that do not. Decoding has to happen
    before the comparison against the body, or every non-ASCII station name would look like
    a mismatch.
    """
    if not (raw.startswith(BASE64_PREFIX) and raw.endswith(BASE64_SUFFIX)):
        return raw
    encoded = raw[len(BASE64_PREFIX) : -len(BASE64_SUFFIX)]
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise HeaderMismatch(f"the {name} header is marked as Base64 and does not decode") from exc


class Headers(Protocol):
    """The one thing this module needs of a header collection, and why it is not a dict.

    Header names are case-insensitive, and `http.server` hands over a mapping that honours
    that. Copying it into a plain dictionary would make `mcp-method` and `Mcp-Method` two
    different headers, which is a rule the transport states and a client is entitled to.
    """

    def get(self, name: str, failobj: Any = None) -> Any: ...


def check_headers(
    headers: Headers,
    method: str,
    params: Mapping[str, Any],
    envelope: Envelope,
) -> None:
    """Hold the mirrored headers to the body they were copied from.

    Absence is a mismatch as much as disagreement is: these headers are required for
    compliance, and a request without them is one an intermediary could not have routed,
    which makes it indistinguishable from a request that was routed on something else.
    """
    version = headers.get("MCP-Protocol-Version")
    if version is None:
        raise HeaderMismatch("the MCP-Protocol-Version header is required and was not given")
    if version != envelope.version:
        raise HeaderMismatch(
            f"the MCP-Protocol-Version header says {version!r} and the body says "
            f"{envelope.version!r}"
        )

    declared = headers.get("Mcp-Method")
    if declared is None:
        raise HeaderMismatch("the Mcp-Method header is required and was not given")
    if declared != method:
        raise HeaderMismatch(
            f"the Mcp-Method header says {declared!r} and the body says {method!r}"
        )

    named = named_by(method, params)
    if named is None:
        return
    given = headers.get("Mcp-Name")
    if given is None:
        raise HeaderMismatch(f"the Mcp-Name header is required for {method} and was not given")
    if decoded_header("Mcp-Name", given) != named:
        raise HeaderMismatch(
            f"the Mcp-Name header does not match the {method} name in the body"
        )


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

    def _error(
        self,
        status: int,
        message: str,
        request_id: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """A transport failure, which is not the same thing as a tool failure.

        A tool that cannot answer returns a result saying so and the conversation
        continues. This is for the cases before a tool is reached: a wrong path, an
        unknown session, an origin that may not ask.
        """
        code = -32600 if status < 500 else -32603
        self._send(
            status,
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
            headers,
        )

    def _refuse(self, error: ProtocolError, request_id: Any = None) -> None:
        """A protocol failure, in the status the revision pairs it with.

        The pairing is load-bearing rather than cosmetic. A dual-era client works out which
        era it reached by attempting a modern request and reading the body of the 400 that
        may come back, so a recognised error here is what stops it falling back to a
        handshake this server would also have answered.
        """
        self._send(
            error.status,
            {"jsonrpc": "2.0", "id": request_id, "error": error.as_error()},
        )

    def _drain(self, length: int) -> None:
        """Read and discard an over-long body, up to the point where courtesy stops."""
        remaining = min(length, MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    def _guard(self) -> bool:
        """The checks every method shares. False means an answer has already been sent."""
        if urlparse(self.path).path != self.path_served:
            self._error(404, f"nothing is served from {self.path}; the endpoint is {self.path_served}")
            return False
        if not origin_is_allowed(self.headers.get("Origin"), self.allowed_origins):
            self._error(403, "this origin may not reach the server")
            return False
        return True

    # Methods -----------------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            # Answered without reading the body, which is the point of checking the length
            # first: draining a body this server has already refused is the denial of
            # service the ceiling exists to prevent. But a keep-alive connection with an
            # unread body on it is a connection whose next read is the tail of this one, so
            # the client is told not to reuse it. Without that the client is still writing
            # when the socket goes away and sees a broken pipe rather than the 413, which
            # leaves it unable to tell an oversized request from a dead server.
            self._drain(length)
            self.close_connection = True
            self._error(
                413,
                "the request body is larger than this server accepts",
                headers={"Connection": "close"},
            )
            return
        try:
            message = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError as exc:
            self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
            return
        if not isinstance(message, dict):
            self._error(400, "a request is one JSON-RPC message; batching was withdrawn from the protocol")
            return

        method, request_id = message.get("method") or "", message.get("id")
        params = message.get("params") or {}

        try:
            envelope = read_envelope(method, params)
            if envelope.modern:
                check_headers(self.headers, method, params, envelope)
        except ProtocolError as exc:
            self._refuse(exc, request_id)
            return

        if envelope.modern:
            # No identifier is minted and none is echoed. A client that still sends one is
            # speaking an older revision of this transport, and the header is ignored
            # rather than refused, which is what the revision asks for.
            #
            # The name is used here for thread affinity, and used again inside the server
            # for the ledger. Naming a conversation therefore leaves this endpoint holding
            # one ledger that is never written to, beside the one that is. That is the
            # price of keeping conversations on separate threads, which is what stops two
            # callers queueing behind each other's network waits, and it is bounded by the
            # session limit rather than growing. Stdio has one thread and needs the
            # server-side split on its own, so the split cannot move out of the server.
            conversation = self.sessions.obtain(envelope.conversation or UNNAMED_CONVERSATION)
            identifier = None
        elif method == "initialize":
            identifier, conversation = self.sessions.open()
        else:
            given = self.headers.get("Mcp-Session-Id")
            version = self.headers.get("MCP-Protocol-Version")
            if version is not None and version not in LEGACY_PROTOCOL_VERSIONS:
                self._refuse(UnsupportedProtocolVersion(version), request_id)
                return
            found = self.sessions.get(given)
            if found is None:
                # 404 rather than 400 is what tells a client to start again by
                # initialising, which is the recovery the handshake revisions describe.
                self._error(404, "no such session; initialize to start one", request_id)
                return
            identifier, conversation = given or "", found

        if request_id is None:
            # A notification or a response. There is nothing to answer and saying so with
            # 202 is what distinguishes it from a request whose result went missing.
            self._send(202)
            return

        try:
            result = conversation.ask(method, params)
        except ProtocolError as exc:
            # The status the modern revision pairs with the failure, but only for a modern
            # request. A handshake client reads 404 as the endpoint being gone rather than
            # as the method being absent, and those revisions never asked for it.
            self._send(
                exc.status if envelope.modern else 200,
                {"jsonrpc": "2.0", "id": request_id, "error": exc.as_error()},
            )
            return
        except Exception as exc:
            self._send(
                200,
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}},
            )
            return

        headers = {"Mcp-Session-Id": identifier} if identifier and method == "initialize" else {}
        self._send(200, {"jsonrpc": "2.0", "id": request_id, "result": result}, headers)

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
