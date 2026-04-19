"""Namecoin name resolution for `.bit` NIP-05 handles.

Supports two backends:

1. **namecoind JSON-RPC** (`NAMECOIN_RPC_URL`) — trustless, requires running
   a Namecoin full node.
2. **ElectrumX** (`NAMECOIN_ELECTRUMX_SERVERS`) — SPV-style. Connects to one
   or more Namecoin ElectrumX servers over TCP+TLS or Secure WebSocket and
   resolves names via the standard Electrum scripthash protocol. Self-signed
   server certificates are accepted via built-in certificate pinning (same
   server set and pins as Amethyst).

If both are configured, RPC is tried first. If only one is set (or neither),
only that path is used. When neither is set, `.bit` handles fail to resolve.

This module is self-contained: only stdlib + aiohttp (already a Dufflepud
dependency) are required.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import ssl
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


# ── public API ─────────────────────────────────────────────────────────────


async def resolve_bit_domain(
    domain: str,
    rpc_url: Optional[str] = None,
    electrumx_servers: Optional[List["ElectrumxServer"]] = None,
    timeout: float = 10.0,
) -> Optional[dict]:
    """Resolve a `.bit` domain's NIP-05 record from the Namecoin blockchain.

    Returns a dict shaped like a standard `/.well-known/nostr.json` payload
    (``{names, relays, nip46}``) on success, or ``None`` on any failure
    (name missing, all servers unreachable, record malformed, etc.).

    Tries RPC first, then ElectrumX servers in order. The first backend to
    return a valid value wins.
    """
    label = _bit_label(domain)
    if label is None:
        return None

    name = f"d/{label}"

    # Try namecoind RPC first if configured.
    if rpc_url:
        try:
            raw_value = await _rpc_name_show(rpc_url, name, timeout)
            if raw_value is not None:
                parsed = _extract_nostr_record(raw_value)
                if parsed is not None:
                    return parsed
        except Exception as exc:
            logger.warning("Namecoin RPC resolution failed for %s: %s", name, exc)

    # Then try each configured ElectrumX server.
    for server in electrumx_servers or []:
        try:
            raw_value = await _electrumx_name_show(server, name, timeout)
            if raw_value is not None:
                parsed = _extract_nostr_record(raw_value)
                if parsed is not None:
                    return parsed
        except NameNotFound:
            # Definitive answer from the blockchain — no need to try more servers.
            return None
        except Exception as exc:
            logger.warning(
                "ElectrumX resolution failed via %s: %s", server.describe(), exc
            )

    return None


@dataclass(frozen=True)
class ElectrumxServer:
    """A single ElectrumX endpoint.

    `scheme` is one of ``tcp+tls``, ``tcp`` (no TLS, strongly discouraged),
    or ``wss`` / ``ws`` for WebSocket transport.
    """

    scheme: str  # 'tcp+tls' | 'tcp' | 'wss' | 'ws'
    host: str
    port: int

    @classmethod
    def parse(cls, url: str) -> "ElectrumxServer":
        """Parse a server URL like ``tcp+tls://host:50002`` or ``wss://host:50004``.

        Bare ``host:port`` is accepted and treated as ``tcp+tls``.
        """
        url = url.strip()
        if "://" not in url:
            host, _, port = url.rpartition(":")
            if not host or not port:
                raise ValueError(f"invalid electrumx url: {url!r}")
            return cls(scheme="tcp+tls", host=host, port=int(port))

        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"tcp+tls", "tcp", "wss", "ws", "ssl"}:
            raise ValueError(f"unsupported electrumx scheme: {scheme!r}")
        if scheme == "ssl":
            scheme = "tcp+tls"  # common Electrum alias
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"electrumx url missing host/port: {url!r}")
        return cls(scheme=scheme, host=parsed.hostname, port=parsed.port)

    def describe(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def parse_servers(spec: str) -> List[ElectrumxServer]:
    """Parse a comma-separated list of server URLs."""
    out: List[ElectrumxServer] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        out.append(ElectrumxServer.parse(piece))
    return out


# Well-known public Namecoin ElectrumX servers (same set Amethyst ships).
DEFAULT_ELECTRUMX_SERVERS: List[ElectrumxServer] = [
    ElectrumxServer("tcp+tls", "electrumx.testls.space", 50002),
    ElectrumxServer("tcp+tls", "nmc2.bitcoins.sk", 57002),
    ElectrumxServer("tcp+tls", "46.229.238.187", 57002),
]


# ── exceptions ─────────────────────────────────────────────────────────────


class NameNotFound(Exception):
    """The name was queried but the blockchain has no record of it."""


class NameExpired(Exception):
    """The name exists but has expired (>36000 blocks since last update)."""


# ── RPC backend ────────────────────────────────────────────────────────────


async def _rpc_name_show(rpc_url: str, name: str, timeout: float) -> Optional[str]:
    """Call `name_show` on namecoind via JSON-RPC.

    Returns the raw string value (not parsed) on success, or ``None`` if the
    name does not exist or the RPC call failed in a recoverable way.
    """
    payload = {
        "jsonrpc": "1.0",
        "id": "dufflepud",
        "method": "name_show",
        "params": [name],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            rpc_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except aiohttp.ContentTypeError:
                return None

    if not isinstance(data, dict):
        return None
    if data.get("error"):
        # Code -4 = "name not found"; treat any error as missing.
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    return result.get("value")


# ── ElectrumX backend ──────────────────────────────────────────────────────


# Namecoin consensus: names expire this many blocks after their last update.
# (chainparams.cpp: consensus.nNameExpirationDepth = 36000; ~250d at ~10min/blk)
NAME_EXPIRE_DEPTH = 36_000

# Namecoin script opcodes (subset).
_OP_NAME_UPDATE = 0x53  # OP_3 repurposed by Namecoin for name updates
_OP_2DROP = 0x6D
_OP_DROP = 0x75
_OP_RETURN = 0x6A
_OP_PUSHDATA1 = 0x4C
_OP_PUSHDATA2 = 0x4D

_PROTOCOL_VERSION = "1.4"
_CLIENT_IDENT = "dufflepud/0.1"


async def _electrumx_name_show(
    server: ElectrumxServer,
    name: str,
    timeout: float,
) -> Optional[str]:
    """Resolve a Namecoin name via ElectrumX and return the raw value string.

    Uses the scripthash-based approach (compatible with stock ElectrumX +
    Namecoin name index). Same algorithm Amethyst uses on Android/iOS:

      1. Build the canonical `OP_NAME_UPDATE <push(name)> <push('')>...` script
      2. Compute the Electrum-style scripthash (reversed SHA-256)
      3. `blockchain.scripthash.get_history` -> latest tx + height
      4. `blockchain.transaction.get` verbose -> parse NAME_UPDATE vout
      5. `blockchain.headers.subscribe` -> current height (for expiry check)

    Raises `NameNotFound` when the blockchain has no history for this name
    (so callers can skip remaining servers). Returns ``None`` for transient
    server errors.
    """

    async def session(proto: "_ElectrumProtocol") -> Optional[str]:
        # Negotiate version (some servers require this before any query).
        await proto.call("server.version", [_CLIENT_IDENT, _PROTOCOL_VERSION])

        name_script = _build_name_index_script(name.encode("ascii"))
        script_hash = _electrum_script_hash(name_script)

        history = await proto.call(
            "blockchain.scripthash.get_history", [script_hash]
        )
        if not isinstance(history, list) or not history:
            raise NameNotFound(name)

        latest = history[-1]
        tx_hash = latest.get("tx_hash")
        height = int(latest.get("height") or 0)
        if not tx_hash:
            raise NameNotFound(name)

        tx = await proto.call("blockchain.transaction.get", [tx_hash, True])
        if not isinstance(tx, dict):
            return None

        # Best-effort expiry check.
        try:
            headers = await proto.call("blockchain.headers.subscribe", [])
            current_height = (
                int(headers.get("height")) if isinstance(headers, dict) else None
            )
            if (
                current_height is not None
                and height > 0
                and current_height - height >= NAME_EXPIRE_DEPTH
            ):
                raise NameExpired(name)
        except NameExpired:
            raise
        except Exception:
            pass  # non-fatal; we'll still return the value

        return _extract_value_from_transaction(name, tx)

    try:
        async with _connect_electrumx(server, timeout) as proto:
            return await asyncio.wait_for(session(proto), timeout=timeout)
    except NameNotFound:
        raise
    except NameExpired:
        raise


# ── transport ──────────────────────────────────────────────────────────────


class _ElectrumProtocol:
    """Thin JSON-RPC-over-line / JSON-RPC-over-WS wrapper."""

    def __init__(self, read, write, *, kind: str):
        self._read = read  # callable() -> str (one JSON line)
        self._write = write  # callable(str) -> None
        self._kind = kind
        self._id = 0

    async def call(self, method: str, params: list) -> Any:
        self._id += 1
        req = (
            json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
            + "\n"
        )
        await self._write(req)
        raw = await self._read()
        if raw is None:
            raise RuntimeError("electrumx closed connection")
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise RuntimeError(f"electrumx malformed response: {raw[:200]}")
        err = envelope.get("error")
        if err:
            raise RuntimeError(f"electrumx error: {err}")
        return envelope.get("result")


class _ConnectionContext:
    """Async context manager wrapping an _ElectrumProtocol + transport cleanup."""

    def __init__(self, enter, exit):
        self._enter = enter
        self._exit = exit
        self._proto: Optional[_ElectrumProtocol] = None

    async def __aenter__(self) -> _ElectrumProtocol:
        self._proto = await self._enter()
        return self._proto

    async def __aexit__(self, exc_type, exc, tb):
        await self._exit()


def _connect_electrumx(server: ElectrumxServer, timeout: float) -> _ConnectionContext:
    if server.scheme in {"tcp+tls", "tcp"}:
        return _connect_tcp(server, timeout)
    if server.scheme in {"wss", "ws"}:
        return _connect_ws(server, timeout)
    raise ValueError(f"unsupported electrumx scheme: {server.scheme}")


def _connect_tcp(server: ElectrumxServer, timeout: float) -> _ConnectionContext:
    state: dict = {}

    async def enter() -> _ElectrumProtocol:
        use_tls = server.scheme == "tcp+tls"
        ssl_ctx = _build_pinned_ssl_context() if use_tls else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=server.host,
                port=server.port,
                ssl=ssl_ctx,
                server_hostname=server.host if use_tls else None,
            ),
            timeout=timeout,
        )
        state["writer"] = writer

        async def read() -> Optional[str]:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                return None
            return line.decode("utf-8", errors="replace")

        async def write(data: str) -> None:
            writer.write(data.encode("utf-8"))
            await writer.drain()

        return _ElectrumProtocol(read, write, kind="tcp")

    async def exit_() -> None:
        writer = state.get("writer")
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    return _ConnectionContext(enter, exit_)


def _connect_ws(server: ElectrumxServer, timeout: float) -> _ConnectionContext:
    state: dict = {}

    async def enter() -> _ElectrumProtocol:
        ssl_ctx = _build_pinned_ssl_context() if server.scheme == "wss" else None
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))
        state["session"] = session
        ws_url = f"{server.scheme}://{server.host}:{server.port}"
        try:
            ws = await session.ws_connect(ws_url, ssl=ssl_ctx)
        except Exception:
            await session.close()
            state.pop("session", None)
            raise
        state["ws"] = ws

        async def read() -> Optional[str]:
            msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
            if msg.type in (aiohttp.WSMsgType.TEXT,):
                return msg.data
            if msg.type == aiohttp.WSMsgType.BINARY:
                return msg.data.decode("utf-8", errors="replace")
            return None

        async def write(data: str) -> None:
            await ws.send_str(data)

        return _ElectrumProtocol(read, write, kind="ws")

    async def exit_() -> None:
        ws = state.get("ws")
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        session = state.get("session")
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass

    return _ConnectionContext(enter, exit_)


# ── TLS pinning ────────────────────────────────────────────────────────────

# PEM certificates for the well-known Namecoin ElectrumX servers. Self-signed
# and cannot be verified against public CAs, so we pin them explicitly.
#
# These are the same pins shipped by Amethyst; regenerate via:
#   echo | openssl s_client -connect HOST:PORT 2>/dev/null | \
#       openssl x509 -outform PEM
_PINNED_ELECTRUMX_CERTS: List[str] = [
    # electrumx.testls.space:50002 -- expires 2027-05-04
    """-----BEGIN CERTIFICATE-----
MIIDwzCCAqsCFGGKT5mjh7oN98aNyjOCiqafL8VyMA0GCSqGSIb3DQEBCwUAMIGd
MQswCQYDVQQGEwJVUzEQMA4GA1UECAwHQ2hpY2FnbzEQMA4GA1UEBwwHQ2hpY2Fn
bzESMBAGA1UECgwJSW50ZXJuZXRzMQ8wDQYDVQQLDAZJbnRlcncxHjAcBgNVBAMM
FWVsZWN0cnVtLnRlc3Rscy5zcGFjZTElMCMGCSqGSIb3DQEJARYWbWpfZ2lsbF84
OUBob3RtYWlsLmNvbTAeFw0yMjA1MDUwNjIzNDFaFw0yNzA1MDQwNjIzNDFaMIGd
MQswCQYDVQQGEwJVUzEQMA4GA1UECAwHQ2hpY2FnbzEQMA4GA1UEBwwHQ2hpY2Fn
bzESMBAGA1UECgwJSW50ZXJuZXRzMQ8wDQYDVQQLDAZJbnRlcncxHjAcBgNVBAMM
FWVsZWN0cnVtLnRlc3Rscy5zcGFjZTElMCMGCSqGSIb3DQEJARYWbWpfZ2lsbF84
OUBob3RtYWlsLmNvbTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAO4H
+PKCdiiz3jNOA77aAmS2YaU7eOQ8ZGliEVr/PlLcgF5gmthb2DI6iK4KhC1ad34G
1n9IhkXPhkVJ94i8wB3uoTBlA7mI5h59m01yhzSkJAoYoU/i6DM9ipbakqWFCTEp
P+yE216NTU5MbYwThZdRSAIIABe9RyIliMSidyrwHvKBLfnJPFScghW6rhBWN7PG
PA8k0MFGzf+HXbpnV/jAvz08ZC34qiBIjkJrTgh49JweyoZKdppyJcH4UbkslJ2t
YUJR3oURBvrPj+D7TwLVRbX36ul7r4+dP3IjgmljsSAHDK4N/PfWrCBdlj9Pc1Cp
yX+ZDh8X2NrL4ukHoVMCAwEAATANBgkqhkiG9w0BAQsFAAOCAQEAeVj6VZNmY/Vb
nhzrC7xBSHqVWQ1wkLOClLsdvgKP8cFFJuUoCMQU5bPMi7nWnkfvvsIKH4Eibk5K
fqiA9jVsY0FHvQ8gP3KMk1LVuUf/sTcRe5itp3guBOSk/zXZUD5tUz/oRk3k+rdc
MsInqhomjNy/dqYmD6Wm4DNPjZh6fWy+AVQKVNOI2t4koaVdpoi8Uv8h4gFGPbdI
sVmtoGiIGkKNIWum+6mnF6PfynNrLk+ztH4TrdacVNeoJUPYEAxOuesWXFy3H4r+
HKBqA4xAzyjgKLPqoWnjSu7gxj1GIjBhnDxkM6wUOnDq8A0EqxR+A17OcXW9sZ2O
2ZIVwmtnyA==
-----END CERTIFICATE-----""",
    # nmc2.bitcoins.sk:57002 / 46.229.238.187:57002 -- expires 2030-10-22
    """-----BEGIN CERTIFICATE-----
MIID+TCCAuGgAwIBAgIUdmJGukmfPvqmAYpTfuGcjRoYHJ8wDQYJKoZIhvcNAQEL
BQAwgYsxCzAJBgNVBAYTAlNLMREwDwYDVQQIDAhTbG92YWtpYTETMBEGA1UEBwwK
QnJhdGlzbGF2YTEUMBIGA1UECgwLYml0Y29pbnMuc2sxGTAXBgNVBAMMEG5tYzIu
Yml0Y29pbnMuc2sxIzAhBgkqhkiG9w0BCQEWFGRlYWZib3lAY2ljb2xpbmEub3Jn
MB4XDTIwMTAyNDE5MjQzOVoXDTMwMTAyMjE5MjQzOVowgYsxCzAJBgNVBAYTAlNL
MREwDwYDVQQIDAhTbG92YWtpYTETMBEGA1UEBwwKQnJhdGlzbGF2YTEUMBIGA1UE
CgwLYml0Y29pbnMuc2sxGTAXBgNVBAMMEG5tYzIuYml0Y29pbnMuc2sxIzAhBgkq
hkiG9w0BCQEWFGRlYWZib3lAY2ljb2xpbmEub3Jn
MIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEAzBUkZNDfaz7kc28l5tDKohJjekWmz1ynzfGx3ZLsqOZE
c+kNfcMaWU+zT/j0mV6pX6KSH7G9pPAku+8PRdKRq+d63wiJDEjGSaFztQWKW6L1
vTxgCK5gu+Eir3BkTagJObsrLKS+T6qH610/3+btGgoR3lunB5TzCgB/9oQanjDW
zjg2CwmxgR5Iw1Eqfenx7zkSK33FSXSF2SvbUs1Atj2oPU4DLivyrx0RaUmaPemn
cmcpnax+py4pQeB6dJWU1INhzXt3hTJRyoqsSGY3vCECIKIBIkh8GsYjAX4z+Y9y
6pJx0da2b88qPWdsoxaIMvrQiuWknDrSJwAyw2Yd8QIDAQABo1MwUTAdBgNVHQ4E
FgQUT2J83B2/9jxGGdFeWrxMohTzHNwwHwYDVR0jBBgwFoAUT2J83B2/9jxGGdFe
WrxMohTzHNwwDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOCAQEAsbxX
wN8tZaXOybImMZCQS7zfxmKl2IAcqu+R01KPfnIfrFqXPsGDDl3rYLkwh1O4/hYQ
NKNW9KTxoJxuBmAkm7EXQQh1XUUzajdEDqDBVRyvR0Z2MdMYnMSAiiMXMl2wUZnc
QXYftBo0HbtfsaJjImQdDjmlmRPSzE/RW6iUe+1cesKBC7e8nVf69Yu/fxO4m083
VWwAstlWJfk1GyU7jzVc8svealg/oIiDoOMe6CFSLx1BDv2FeHSpRdqd3fn+AC73
bK2N2smrHUOQnFijuiFw3WOrjERi0eMhjVNfVu9W9ZYa/Wd6SdIzV55LbG+NpmSf
5W7ix41hRvdT6cTAJA==
-----END CERTIFICATE-----""",
]


_pinned_ssl_context_cache: Optional[ssl.SSLContext] = None


def _build_pinned_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext that trusts the pinned ElectrumX certs + system CAs.

    Hostname verification is disabled because several pinned certs list a
    single CN but also serve requests by IP (e.g. ``46.229.238.187`` uses
    the ``nmc2.bitcoins.sk`` cert). Cert identity is instead established by
    the pinned trust store itself: only the explicit certs in
    `_PINNED_ELECTRUMX_CERTS` (or the system CAs) will verify.
    """
    global _pinned_ssl_context_cache
    if _pinned_ssl_context_cache is not None:
        return _pinned_ssl_context_cache

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED

    # System CAs (for real certs).
    try:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    except Exception:
        pass

    # Pinned self-signed certs.
    for pem in _PINNED_ELECTRUMX_CERTS:
        try:
            ctx.load_verify_locations(cadata=pem)
        except ssl.SSLError as exc:
            logger.warning("Skipping malformed pinned ElectrumX cert: %s", exc)

    _pinned_ssl_context_cache = ctx
    return ctx


# ── Namecoin script parsing ───────────────────────────────────────────────


def _build_name_index_script(name_bytes: bytes) -> bytes:
    """Canonical script that ElectrumX indexes for a Namecoin name.

    Matches `build_name_index_script` in the Namecoin ElectrumX fork:
      OP_NAME_UPDATE <push(name)> <push(empty)> OP_2DROP OP_DROP OP_RETURN
    """
    out = bytearray([_OP_NAME_UPDATE])
    out += _push_data(name_bytes)
    out += _push_data(b"")  # empty value
    out += bytes([_OP_2DROP, _OP_DROP, _OP_RETURN])
    return bytes(out)


def _push_data(data: bytes) -> bytes:
    n = len(data)
    if n < 0x4C:
        return bytes([n]) + data
    if n <= 0xFF:
        return bytes([_OP_PUSHDATA1, n]) + data
    if n <= 0xFFFF:
        return bytes([_OP_PUSHDATA2, n & 0xFF, (n >> 8) & 0xFF]) + data
    raise ValueError(f"push data too large: {n}")


def _electrum_script_hash(script: bytes) -> str:
    """Electrum scripthash: SHA-256 of the script, byte-reversed, hex-encoded."""
    digest = hashlib.sha256(script).digest()
    return digest[::-1].hex()


def _extract_value_from_transaction(name: str, tx: dict) -> Optional[str]:
    """Scan a verbose tx for a NAME_UPDATE vout matching ``name`` and return the value."""
    vouts = tx.get("vout") or []
    for vout in vouts:
        script_hex = (
            vout.get("scriptPubKey", {}).get("hex")
            if isinstance(vout, dict)
            else None
        )
        if not isinstance(script_hex, str):
            continue
        # NAME_UPDATE scripts start with OP_3 (0x53).
        if not script_hex.startswith("53"):
            continue
        try:
            script_bytes = bytes.fromhex(script_hex)
        except ValueError:
            continue
        parsed = _parse_name_script(script_bytes)
        if parsed is None:
            continue
        found_name, value = parsed
        if found_name == name:
            return value
    return None


def _parse_name_script(script: bytes) -> Optional[Tuple[str, str]]:
    if not script or script[0] != _OP_NAME_UPDATE:
        return None
    pos = 1
    name_bytes, pos = _read_push_data(script, pos)
    if name_bytes is None:
        return None
    value_bytes, _ = _read_push_data(script, pos)
    if value_bytes is None:
        return None
    try:
        name = name_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None
    # Values are often UTF-8 JSON; fall back to latin-1 to avoid losing bytes.
    try:
        value = value_bytes.decode("utf-8")
    except UnicodeDecodeError:
        value = value_bytes.decode("latin-1")
    return name, value


def _read_push_data(script: bytes, pos: int) -> Tuple[Optional[bytes], int]:
    if pos >= len(script):
        return None, pos
    opcode = script[pos]
    if opcode == 0:
        return b"", pos + 1
    if opcode < 0x4C:
        end = pos + 1 + opcode
        if end > len(script):
            return None, pos
        return script[pos + 1 : end], end
    if opcode == _OP_PUSHDATA1:
        if pos + 2 > len(script):
            return None, pos
        length = script[pos + 1]
        end = pos + 2 + length
        if end > len(script):
            return None, pos
        return script[pos + 2 : end], end
    if opcode == _OP_PUSHDATA2:
        if pos + 3 > len(script):
            return None, pos
        length = script[pos + 1] | (script[pos + 2] << 8)
        end = pos + 3 + length
        if end > len(script):
            return None, pos
        return script[pos + 3 : end], end
    return None, pos


# ── shared helpers ────────────────────────────────────────────────────────


def _bit_label(domain: str) -> Optional[str]:
    """Strip and validate the `.bit` label."""
    if not domain.lower().endswith(".bit"):
        return None
    label = domain[:-4].lower()
    if not label or "/" in label or "\0" in label:
        return None
    return label


def _extract_nostr_record(raw_value: str) -> Optional[dict]:
    """Parse a Namecoin `d/<name>` value and pull out the NIP-05 payload.

    Accepts both layouts:
      * ``value.nostr = {names, relays, nip46}`` (preferred; coexists with
        `ip`, `map`, etc.)
      * ``value = {names, relays, nip46}`` (value is the bare record)
    """
    if not raw_value:
        return None
    try:
        value = json.loads(raw_value)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    nostr = value.get("nostr")
    if isinstance(nostr, dict) and "names" in nostr:
        return nostr
    if "names" in value:
        return value
    return None
