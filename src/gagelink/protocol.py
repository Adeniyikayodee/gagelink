"""Which revision of MCP a request is speaking, and what that revision requires.

The protocol split in two on 2026-07-28. Every revision up to and including 2025-11-25
opens with an `initialize` handshake and then treats the connection as a conversation:
the version, the client's identity, and its capabilities are agreed once and remembered.
The revision this module calls modern removes all of that. There is no handshake, a
request carries its own version and capabilities in `_meta`, and a server may not infer
anything from what came before it on the same connection.

The two cannot be told apart by the method name, because `tools/call` exists in both. They
are told apart by whether the request carries `io.modelcontextprotocol/protocolVersion`,
which is exactly what the specification says a dual-era server should key on: a request
carrying modern per-request metadata is served statelessly, and an `initialize` request
selects legacy semantics. Anything else is legacy, which is the safe reading, because a
legacy client's `tools/list` looks identical to a modern one with its metadata stripped
and answering it in the modern form would tell it about fields it cannot read.

Supporting both is not politeness. A modern client against a legacy-only server fails
outright: on stdio the probe returns an unrecognised error and the client gives up, and on
HTTP the request is missing the headers the server never learned to read. So a server that
stays on the handshake stops answering as its clients update, and one that moves to the
modern revision alone stops answering the clients that have not.

Nothing here holds state. That is the point of the revision, and it is also why the
conversation identifier below exists: the ledger this package keeps has to span several
requests, and the modern protocol says such state must be named by an identifier the
client passes each time rather than inferred from the connection underneath.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

#: The revision that removed the handshake. A request declaring this is served statelessly.
MODERN_VERSION = "2026-07-28"

#: The handshake revisions, newest first. A client asking `initialize` for one of these is
#: answered in it; a client asking for anything else is answered in the newest, which is
#: what those revisions say to do. Nothing here differs between the three except the fields
#: a client may send, which are additive, so supporting the older two costs nothing.
LEGACY_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: What `initialize` answers when it cannot answer in the revision it was asked for.
LEGACY_PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSIONS[0]

#: Everything this server speaks, newest first. This is the list `server/discover` returns
#: and the list an unsupported-version error names.
SUPPORTED_VERSIONS = (MODERN_VERSION,) + LEGACY_PROTOCOL_VERSIONS

#: Reserved `_meta` keys. The prefix belongs to the specification; a server inventing a key
#: under it is claiming to implement something it does not.
PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"

#: This package's own `_meta` key, under a prefix the specification leaves to us: a prefix
#: is reserved only when its second label is `modelcontextprotocol` or `mcp`, and ours is
#: `github`. It names the conversation a request belongs to.
#:
#: The modern revision is explicit that an open connection is not a conversation, and that
#: a client may interleave unrelated work on one stdio process. A manifest that spans a
#: conversation therefore cannot be scoped to the process. A client that sends this key
#: gets a ledger of its own; one that does not shares the default, which is the same
#: behaviour a single-conversation client had before and is stated rather than assumed.
CONVERSATION_KEY = "io.github.adeniyikayodee.gagelink/conversation"

#: JSON-RPC codes. The first three are the specification's own reserved sub-range, which is
#: why they may not be used for anything else, and why -32002 is gone: the modern revision
#: forbids emitting it and uses -32602 for a resource that is not there.
HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

#: A result that is finished, as opposed to one asking the client for more input. Every
#: modern result carries this. A client reading a result without one is required to treat
#: it as complete, which is what keeps legacy results readable.
COMPLETE = "complete"


class ProtocolError(Exception):
    """A failure in the protocol rather than in a tool.

    Carries the HTTP status the transport should use, because the modern revision fixes
    the pairing: an unsupported version, a missing capability, and a header that disagrees
    with the body are all 400, and an unimplemented method is 404. A client distinguishes a
    modern server from a legacy one by reading the body of exactly those responses, so
    sending the wrong status makes a working server look like an absent one.
    """

    code: int = INVALID_PARAMS
    status: int = 400

    def __init__(self, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    def as_error(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            body["data"] = self.data
        return body


class UnsupportedProtocolVersion(ProtocolError):
    """A revision this server does not speak.

    The error names what this server does speak, which is the whole mechanism by which a
    client recovers: there is no handshake left to negotiate in, so the failure of the
    first request has to carry enough for the second one to succeed.
    """

    code = UNSUPPORTED_PROTOCOL_VERSION

    def __init__(self, requested: Any) -> None:
        super().__init__(
            "unsupported protocol version",
            {"supported": list(SUPPORTED_VERSIONS), "requested": requested},
        )


class MalformedRequest(ProtocolError):
    """A modern request missing something every modern request must carry."""

    code = INVALID_PARAMS


class HeaderMismatch(ProtocolError):
    """An HTTP header that disagrees with the body it was mirrored from.

    Worth refusing rather than resolving. The headers exist so that a load balancer can
    route without reading the body, which means a request where the two disagree is one
    where the thing that routed it and the thing that runs it were working from different
    instructions. Picking either one is a guess about which.
    """

    code = HEADER_MISMATCH


class MethodNotFound(ProtocolError):
    """An unsupported JSON-RPC method.

    404 rather than 400, so that a client probing for a capability can tell an unimplemented
    method from a malformed request, and -32601 in the body so it can tell this server from
    an HTTP+SSE server that does not host a modern endpoint at all.
    """

    code = METHOD_NOT_FOUND
    status = 404

    def __init__(self, method: str) -> None:
        super().__init__(f"method not found: {method}")


@dataclass(frozen=True)
class Envelope:
    """What a request says about itself, before any of it reaches a tool."""

    #: Whether this request is served statelessly, by the modern revision's rules.
    modern: bool
    #: The revision the request declared, or the newest legacy one where it declared none.
    version: str
    #: The conversation whose ledger this request belongs to, where the client named one.
    conversation: str | None = None
    #: Self-reported, unverified, and for display only. Never read to decide anything.
    client_info: Mapping[str, Any] | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)


def read_envelope(method: str, params: Mapping[str, Any]) -> Envelope:
    """Decide which revision a request is speaking, and check it against that revision.

    A request declaring a version is held to the modern rules, including the requirement
    that it declare its capabilities: a server may not rely on a capability the client did
    not state, so a request that states none has not said enough to be answered.
    """
    meta = params.get("_meta")
    meta = meta if isinstance(meta, Mapping) else {}

    declared = meta.get(PROTOCOL_VERSION_KEY)
    if declared is None:
        # No per-request metadata, so legacy semantics, which is what `initialize` selects
        # and what every request after it inherits.
        return Envelope(modern=False, version=LEGACY_PROTOCOL_VERSION)

    if not isinstance(declared, str) or declared not in SUPPORTED_VERSIONS:
        raise UnsupportedProtocolVersion(declared)
    if declared in LEGACY_PROTOCOL_VERSIONS:
        # A handshake revision named in per-request metadata. The two eras disagree about
        # what a request means, so this is refused rather than guessed at.
        raise UnsupportedProtocolVersion(declared)

    capabilities = meta.get(CLIENT_CAPABILITIES_KEY)
    if not isinstance(capabilities, Mapping):
        raise MalformedRequest(
            f"{CLIENT_CAPABILITIES_KEY} is required on every request of {declared} and "
            f"was not given"
        )

    client_info = meta.get(CLIENT_INFO_KEY)
    conversation = meta.get(CONVERSATION_KEY)
    return Envelope(
        modern=True,
        version=declared,
        conversation=conversation if isinstance(conversation, str) and conversation else None,
        client_info=client_info if isinstance(client_info, Mapping) else None,
        capabilities=capabilities,
    )


def named_by(method: str, params: Mapping[str, Any]) -> str | None:
    """The value the `Mcp-Name` header mirrors, for the methods that have one.

    Only these three carry a name. For anything else the header is neither required nor
    checked, and a client sending one is not wrong, only redundant.
    """
    if method == "tools/call":
        name = params.get("name")
    elif method == "resources/read":
        name = params.get("uri")
    elif method == "prompts/get":
        name = params.get("name")
    else:
        return None
    return name if isinstance(name, str) else None


__all__ = [
    "CLIENT_CAPABILITIES_KEY",
    "CLIENT_INFO_KEY",
    "COMPLETE",
    "CONVERSATION_KEY",
    "Envelope",
    "HEADER_MISMATCH",
    "HeaderMismatch",
    "LEGACY_PROTOCOL_VERSION",
    "LEGACY_PROTOCOL_VERSIONS",
    "MODERN_VERSION",
    "MalformedRequest",
    "MethodNotFound",
    "PROTOCOL_VERSION_KEY",
    "ProtocolError",
    "SERVER_INFO_KEY",
    "SUPPORTED_VERSIONS",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "UnsupportedProtocolVersion",
    "named_by",
    "read_envelope",
]
