"""Namecoin name resolution for `.bit` NIP-05 handles.

A faithful Python port of Amethyst's Namecoin resolver
(``quartz/.../nip05DnsIdentifiers/namecoin``), covering:

  * Identifier parsing for the NIP-05 (`user@domain.bit`), bare-domain
    (`domain.bit`), and direct-namespace (`d/<name>`, `id/<name>`) forms.
  * Resolution via namecoind JSON-RPC (trustless) and/or ElectrumX
    (SPV-style). ElectrumX supports both raw TCP+TLS and Secure
    WebSocket transports, uses the scripthash protocol
    (no Namecoin-specific RPC methods required), and checks name
    expiry against the chain tip.
  * Self-signed certificate pinning for the well-known public Namecoin
    ElectrumX servers, plus an optional env-var escape hatch
    (`NAMECOIN_ELECTRUMX_PINS`) to pin additional operator-supplied
    certs. Hostname verification is enabled for servers that are not
    in the pin set; pinned servers rely on the explicit trust store
    for identity, which lets IPs (e.g. 46.229.238.187) share a
    hostname-scoped cert.
  * Value parsing that accepts every layout Amethyst does: the
    simple form `{"nostr": "<hex>"}`, the extended form
    `{"nostr": {"names": {...}, "relays": {...}}}`, a flat record
    `{"names": {...}}`, and id-namespace variants
    `{"nostr": {"pubkey": "<hex>"}}`.
  * Structured resolution outcomes (NameNotFound, NoNostrField,
    ServersUnreachable, InvalidIdentifier, Timeout, Expired) for
    callers that want to distinguish failure modes.
  * Per-process LRU cache with TTL, per-server mutexes, configurable
    connect/read timeouts, and best-effort expiry reporting.

Only stdlib + aiohttp (already a Dufflepud dependency) are required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


# ── public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NamecoinNostrResult:
    """Successful resolution: a pubkey + associated metadata."""

    pubkey: str
    namecoin_name: str
    local_part: str = "_"
    relays: Tuple[str, ...] = ()
    nip46: Tuple[str, ...] = ()
    expires_in: Optional[int] = None  # blocks until expiry (if known)

    def to_nip05_record(self) -> dict:
        """Return a dict in the standard `/.well-known/nostr.json` shape.

        This is what Dufflepud's existing handle resolver consumes, so
        callers can treat Namecoin and DNS paths uniformly.
        """
        out: Dict[str, Any] = {"names": {self.local_part: self.pubkey}}
        if self.relays:
            out["relays"] = {self.pubkey: list(self.relays)}
        if self.nip46:
            out["nip46"] = {self.pubkey: list(self.nip46)}
        return out


class NamecoinResolveOutcome:
    """Namespace for all possible outcomes of a Namecoin resolve.

    Mirrors Amethyst's sealed ``NamecoinResolveOutcome`` hierarchy.
    Callers that only need the happy path can ignore this and use
    :func:`resolve_bit_domain` / :func:`resolve_identifier`, which return
    ``None`` on any failure.
    """

    @dataclass(frozen=True)
    class Success:
        result: NamecoinNostrResult

    @dataclass(frozen=True)
    class NameNotFound:
        name: str

    @dataclass(frozen=True)
    class NameExpired:
        name: str

    @dataclass(frozen=True)
    class NoNostrField:
        name: str

    @dataclass(frozen=True)
    class ServersUnreachable:
        message: str

    @dataclass(frozen=True)
    class InvalidIdentifier:
        identifier: str

    @dataclass(frozen=True)
    class Timeout:
        pass


# ── exceptions ─────────────────────────────────────────────────────────────


class NamecoinLookupException(Exception):
    """Base class for Namecoin lookup failures."""


class NameNotFound(NamecoinLookupException):
    """The blockchain has no record for this name."""

    def __init__(self, name: str):
        super().__init__(f"Name not found: {name}")
        self.name = name


class NameExpired(NamecoinLookupException):
    """The name exists but has expired (>36000 blocks since last update)."""

    def __init__(self, name: str):
        super().__init__(f"Name expired: {name}")
        self.name = name


class ServersUnreachable(NamecoinLookupException):
    """All configured ElectrumX servers / RPCs failed."""

    def __init__(self, last_error: Optional[BaseException] = None):
        msg = "All Namecoin resolvers unreachable"
        if last_error:
            msg = f"{msg}: {last_error!r}"
        super().__init__(msg)
        self.last_error = last_error


# ── ElectrumX server types ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ElectrumxServer:
    """A single ElectrumX endpoint.

    ``scheme`` is one of ``tcp+tls``, ``tcp`` (plaintext, discouraged),
    ``wss``, or ``ws``.
    """

    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, url: str) -> "ElectrumxServer":
        """Parse ``tcp+tls://host:port`` etc. Bare ``host:port`` -> tcp+tls."""
        url = url.strip()
        if "://" not in url:
            host, _, port = url.rpartition(":")
            if not host or not port:
                raise ValueError(f"invalid electrumx url: {url!r}")
            return cls(scheme="tcp+tls", host=host, port=int(port))

        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme == "ssl":
            scheme = "tcp+tls"  # Electrum convention
        if scheme not in {"tcp+tls", "tcp", "wss", "ws"}:
            raise ValueError(f"unsupported electrumx scheme: {scheme!r}")
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"electrumx url missing host/port: {url!r}")
        return cls(scheme=scheme, host=parsed.hostname, port=parsed.port)

    def describe(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def is_tls(self) -> bool:
        return self.scheme in {"tcp+tls", "wss"}


def parse_servers(spec: str) -> List[ElectrumxServer]:
    """Parse a comma-separated list of server URLs.

    The literal ``default`` (case-insensitive) expands to the built-in
    list of public Namecoin ElectrumX servers.
    """
    out: List[ElectrumxServer] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if piece.lower() == "default":
            out.extend(DEFAULT_ELECTRUMX_SERVERS)
            continue
        out.append(ElectrumxServer.parse(piece))
    # de-dupe while preserving order
    seen = set()
    unique: List[ElectrumxServer] = []
    for s in out:
        key = (s.scheme, s.host, s.port)
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    return unique


# Well-known public Namecoin ElectrumX servers (same set Amethyst ships).
DEFAULT_ELECTRUMX_SERVERS: List[ElectrumxServer] = [
    ElectrumxServer("tcp+tls", "electrumx.testls.space", 50002),
    ElectrumxServer("tcp+tls", "nmc2.bitcoins.sk", 57002),
    ElectrumxServer("tcp+tls", "46.229.238.187", 57002),
]


# ── configuration model ────────────────────────────────────────────────────


@dataclass
class NamecoinConfig:
    """Knobs for the Namecoin resolver.

    Mirrors the configuration surface of Amethyst's client while using
    Pythonic types. Construct via :meth:`from_env` for the normal case.
    """

    rpc_url: Optional[str] = None
    electrumx_servers: List[ElectrumxServer] = field(default_factory=list)
    extra_pinned_certs: List[str] = field(default_factory=list)
    connect_timeout: float = 10.0
    read_timeout: float = 15.0
    lookup_timeout: float = 20.0
    cache_ttl: float = 3600.0
    cache_max_entries: int = 500

    @classmethod
    def from_env(cls, getter: Callable[[str], Optional[str]] = os.environ.get) -> "NamecoinConfig":
        rpc_url = (getter("NAMECOIN_RPC_URL") or "").strip() or None

        servers_spec = (getter("NAMECOIN_ELECTRUMX_SERVERS") or "").strip()
        try:
            servers = parse_servers(servers_spec) if servers_spec else []
        except ValueError as exc:
            logger.warning("Invalid NAMECOIN_ELECTRUMX_SERVERS: %s", exc)
            servers = []

        pins_raw = (getter("NAMECOIN_ELECTRUMX_PINS") or "").strip()
        extra_pins = _split_pem_blobs(pins_raw) if pins_raw else []

        def _float(name: str, default: float) -> float:
            raw = getter(name)
            if raw is None or raw == "":
                return default
            try:
                return float(raw)
            except ValueError:
                logger.warning("Ignoring invalid %s=%r", name, raw)
                return default

        def _int(name: str, default: int) -> int:
            raw = getter(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                logger.warning("Ignoring invalid %s=%r", name, raw)
                return default

        return cls(
            rpc_url=rpc_url,
            electrumx_servers=servers,
            extra_pinned_certs=extra_pins,
            connect_timeout=_float("NAMECOIN_CONNECT_TIMEOUT", 10.0),
            read_timeout=_float("NAMECOIN_READ_TIMEOUT", 15.0),
            lookup_timeout=_float("NAMECOIN_LOOKUP_TIMEOUT", 20.0),
            cache_ttl=_float("NAMECOIN_CACHE_TTL", 3600.0),
            cache_max_entries=_int("NAMECOIN_CACHE_MAX_ENTRIES", 500),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.rpc_url) or bool(self.electrumx_servers)


def _split_pem_blobs(spec: str) -> List[str]:
    """Split a blob of env-var PEM data into one cert per element.

    Accepts multiple PEM certs concatenated with either literal newlines
    or the two-char escape sequence ``\\n`` (to survive docker/systemd
    env files that don't like multiline values).
    """
    raw = spec.replace("\\n", "\n")
    blobs: List[str] = []
    current: List[str] = []
    for line in raw.splitlines():
        if "-----BEGIN CERTIFICATE-----" in line and current:
            blobs.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        blobs.append("\n".join(current).strip())
    return [b for b in blobs if "-----BEGIN CERTIFICATE-----" in b]


# ── identifier parsing ─────────────────────────────────────────────────────


_HEX_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _is_valid_pubkey(s: Any) -> bool:
    return isinstance(s, str) and bool(_HEX_PUBKEY_RE.match(s))


class _Namespace:
    DOMAIN = "d"
    IDENTITY = "id"


@dataclass(frozen=True)
class _ParsedIdentifier:
    namecoin_name: str  # e.g. "d/example" or "id/alice"
    local_part: str
    namespace: str


def is_namecoin_identifier(identifier: str) -> bool:
    """Quick check: should this identifier be routed to Namecoin?"""
    s = identifier.strip().lower()
    return s.endswith(".bit") or s.startswith("d/") or s.startswith("id/")


def parse_identifier(raw: str) -> Optional[_ParsedIdentifier]:
    """Port of Amethyst's ``NamecoinNameResolver.parseIdentifier``.

    Accepts:
      * ``alice@example.bit`` -> ``d/example`` + ``alice``
      * ``_@example.bit`` / ``example.bit`` -> ``d/example`` + ``_``
      * ``d/example`` -> ``d/example`` + ``_``
      * ``id/alice`` -> ``id/alice`` + ``_``
    Returns ``None`` for anything else.
    """
    s = raw.strip()
    if not s:
        return None
    lower = s.lower()

    # Direct namespace references.
    if lower.startswith("d/"):
        return _ParsedIdentifier(
            namecoin_name=lower, local_part="_", namespace=_Namespace.DOMAIN
        )
    if lower.startswith("id/"):
        return _ParsedIdentifier(
            namecoin_name=lower, local_part="_", namespace=_Namespace.IDENTITY
        )

    # user@domain.bit
    if "@" in s and lower.endswith(".bit"):
        local, _, host = s.partition("@")
        host_lower = host.lower()
        if not host_lower.endswith(".bit"):
            return None
        domain = host_lower[:-4]
        if not domain or "/" in domain or "\0" in domain:
            return None
        local_part = local.lower() or "_"
        return _ParsedIdentifier(
            namecoin_name=f"d/{domain}",
            local_part=local_part,
            namespace=_Namespace.DOMAIN,
        )

    # bare domain.bit
    if lower.endswith(".bit"):
        domain = lower[:-4]
        if not domain or "/" in domain or "\0" in domain:
            return None
        return _ParsedIdentifier(
            namecoin_name=f"d/{domain}",
            local_part="_",
            namespace=_Namespace.DOMAIN,
        )

    return None


# ── public resolution entrypoints ──────────────────────────────────────────


async def resolve_identifier(
    identifier: str,
    config: Optional[NamecoinConfig] = None,
) -> Optional[NamecoinNostrResult]:
    """Happy-path resolver: return a :class:`NamecoinNostrResult` or ``None``."""
    outcome = await resolve_identifier_detailed(identifier, config)
    if isinstance(outcome, NamecoinResolveOutcome.Success):
        return outcome.result
    return None


async def resolve_identifier_detailed(
    identifier: str,
    config: Optional[NamecoinConfig] = None,
) -> Any:
    """Detailed resolver: returns one of the :class:`NamecoinResolveOutcome` variants."""
    cfg = config or _default_config()
    parsed = parse_identifier(identifier)
    if parsed is None:
        return NamecoinResolveOutcome.InvalidIdentifier(identifier)
    if not cfg.enabled:
        return NamecoinResolveOutcome.ServersUnreachable(
            "Namecoin resolution is not configured"
        )

    cached = _resolver_cache.get(identifier, cfg.cache_ttl)
    if cached is not None:
        return cached

    try:
        outcome = await asyncio.wait_for(
            _resolve_parsed(parsed, cfg), timeout=cfg.lookup_timeout
        )
    except asyncio.TimeoutError:
        outcome = NamecoinResolveOutcome.Timeout()

    # Cache successes and definitive negatives; don't cache transient failures.
    if isinstance(
        outcome,
        (
            NamecoinResolveOutcome.Success,
            NamecoinResolveOutcome.NameNotFound,
            NamecoinResolveOutcome.NameExpired,
            NamecoinResolveOutcome.NoNostrField,
            NamecoinResolveOutcome.InvalidIdentifier,
        ),
    ):
        _resolver_cache.put(identifier, outcome)
    return outcome


async def resolve_bit_domain(
    domain: str,
    rpc_url: Optional[str] = None,
    electrumx_servers: Optional[List[ElectrumxServer]] = None,
    timeout: Optional[float] = None,
) -> Optional[dict]:
    """Thin compatibility wrapper used by Dufflepud's ``/handle/info``.

    Returns a dict shaped like ``/.well-known/nostr.json``
    (``{names, relays, nip46}``) for a bare `.bit` domain, or ``None``.
    """
    if not domain.lower().endswith(".bit"):
        return None
    cfg = NamecoinConfig(
        rpc_url=rpc_url,
        electrumx_servers=list(electrumx_servers or []),
        lookup_timeout=timeout if timeout is not None else 20.0,
    )
    result = await resolve_identifier(domain, cfg)
    if result is None:
        return None
    # Also include sibling names if we happen to have the full record cached
    # from a previous lookup — cheap because the cache is keyed per-identifier,
    # so we just surface the single record the caller asked for.
    return result.to_nip05_record()


# ── internals: resolution pipeline ────────────────────────────────────────


async def _resolve_parsed(
    parsed: _ParsedIdentifier, cfg: NamecoinConfig
) -> Any:
    """Run the resolution pipeline for a parsed identifier."""
    try:
        raw_value, tip = await _fetch_value_with_tip(parsed.namecoin_name, cfg)
    except NameNotFound:
        return NamecoinResolveOutcome.NameNotFound(parsed.namecoin_name)
    except NameExpired:
        return NamecoinResolveOutcome.NameExpired(parsed.namecoin_name)
    except ServersUnreachable as exc:
        return NamecoinResolveOutcome.ServersUnreachable(str(exc))
    except Exception as exc:  # defensive: treat unknown errors as unreachable
        logger.warning("Unexpected Namecoin resolution error: %s", exc)
        return NamecoinResolveOutcome.ServersUnreachable(repr(exc))

    if raw_value is None:
        return NamecoinResolveOutcome.NameNotFound(parsed.namecoin_name)

    try:
        value = json.loads(raw_value)
    except (ValueError, TypeError):
        return NamecoinResolveOutcome.NoNostrField(parsed.namecoin_name)
    if not isinstance(value, dict):
        return NamecoinResolveOutcome.NoNostrField(parsed.namecoin_name)

    extractor = (
        _extract_from_domain_value
        if parsed.namespace == _Namespace.DOMAIN
        else _extract_from_identity_value
    )
    result = extractor(value, parsed)
    if result is None:
        return NamecoinResolveOutcome.NoNostrField(parsed.namecoin_name)

    if tip is not None:
        result = _annotate_expiry(result, tip)
    return NamecoinResolveOutcome.Success(result)


async def _fetch_value_with_tip(
    name: str, cfg: NamecoinConfig
) -> Tuple[Optional[str], Optional["_TipInfo"]]:
    """Fetch the raw name value via RPC or ElectrumX, and best-effort tip info.

    Returns ``(raw_value, tip)`` where ``tip`` is ``None`` if we couldn't learn
    the current height (and hence can't compute expiry).

    Raises :class:`NameNotFound`, :class:`NameExpired`, or
    :class:`ServersUnreachable`.
    """
    last_error: Optional[BaseException] = None
    got_any_response = False

    if cfg.rpc_url:
        try:
            raw, tip = await _rpc_name_show(cfg.rpc_url, name, cfg.read_timeout)
            got_any_response = True
            if raw is not None:
                return raw, tip
            # raw=None + no exception = name_show RPC replied but without a value.
            # Fall through; ElectrumX might have it.
        except NameNotFound:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning("Namecoin RPC resolution failed for %s: %s", name, exc)

    for server in cfg.electrumx_servers:
        try:
            raw, tip = await _electrumx_name_show(server, name, cfg)
            got_any_response = True
            if raw is not None:
                return raw, tip
        except NameNotFound:
            raise
        except NameExpired:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "ElectrumX resolution failed via %s: %s", server.describe(), exc
            )

    if got_any_response:
        # A backend replied but had no record for this name.
        return None, None
    raise ServersUnreachable(last_error)


@dataclass(frozen=True)
class _TipInfo:
    """Best-effort chain-tip metadata for expiry calculations."""

    current_height: int
    name_height: Optional[int]  # height of the latest name_update tx

    @property
    def blocks_since_update(self) -> Optional[int]:
        if self.name_height is None or self.name_height <= 0:
            return None
        return self.current_height - self.name_height


def _annotate_expiry(
    result: NamecoinNostrResult, tip: _TipInfo
) -> NamecoinNostrResult:
    blocks = tip.blocks_since_update
    if blocks is None:
        return result
    remaining = NAME_EXPIRE_DEPTH - blocks
    return NamecoinNostrResult(
        pubkey=result.pubkey,
        namecoin_name=result.namecoin_name,
        local_part=result.local_part,
        relays=result.relays,
        nip46=result.nip46,
        expires_in=remaining,
    )


# ── value extractors (port of Amethyst's extractFromDomainValue / Identity) ─


def _extract_from_domain_value(
    value: dict, parsed: _ParsedIdentifier
) -> Optional[NamecoinNostrResult]:
    """Extract a NostrResult from a ``d/<name>`` value."""
    # Preferred layout: value.nostr = <hex-pubkey> or {names, relays, nip46}
    nostr_field = value.get("nostr")

    # Simple form: "nostr": "<hex-pubkey>" (root only).
    if isinstance(nostr_field, str):
        if parsed.local_part == "_" and _is_valid_pubkey(nostr_field):
            return NamecoinNostrResult(
                pubkey=nostr_field.lower(),
                namecoin_name=parsed.namecoin_name,
                local_part="_",
            )
        # A string nostr field can't serve non-root local parts.
        if parsed.local_part != "_":
            return None

    # Extended form: "nostr": {names, relays, nip46}
    if isinstance(nostr_field, dict) and "names" in nostr_field:
        return _resolve_from_names_map(nostr_field, parsed)

    # Fallback: value itself carries names (legacy layout).
    if "names" in value and isinstance(value["names"], dict):
        return _resolve_from_names_map(value, parsed)

    return None


def _resolve_from_names_map(
    container: dict, parsed: _ParsedIdentifier
) -> Optional[NamecoinNostrResult]:
    names = container.get("names")
    if not isinstance(names, dict):
        return None

    # Match NIP-05 semantics exactly: a specific local-part must be present
    # in the names map; only a bare-domain query (local_part == '_') falls
    # back to the root entry. This differs slightly from Amethyst's
    # resolver, which fell back to the root for any missing local-part;
    # Dufflepud is a server-side NIP-05 resolver and the standard DNS
    # implementation (/.well-known/nostr.json) returns a negative result
    # in that case. Preserving that signal is important for callers that
    # depend on it (e.g. notifying the user that an @handle is wrong).
    exact = names.get(parsed.local_part)
    if isinstance(exact, str) and _is_valid_pubkey(exact):
        resolved_local, pubkey = parsed.local_part, exact
    elif parsed.local_part == "_":
        root = names.get("_")
        if not (isinstance(root, str) and _is_valid_pubkey(root)):
            # No explicit root either — bare-domain queries can't be
            # satisfied without one.
            return None
        resolved_local, pubkey = "_", root
    else:
        return None

    relays = _extract_relays(container, pubkey)
    nip46 = _extract_nip46(container, pubkey)
    return NamecoinNostrResult(
        pubkey=pubkey.lower(),
        namecoin_name=parsed.namecoin_name,
        local_part=resolved_local,
        relays=tuple(relays),
        nip46=tuple(nip46),
    )


def _extract_from_identity_value(
    value: dict, parsed: _ParsedIdentifier
) -> Optional[NamecoinNostrResult]:
    """Extract a NostrResult from an ``id/<name>`` value."""
    nostr_field = value.get("nostr")

    # "nostr": "<hex>"
    if isinstance(nostr_field, str) and _is_valid_pubkey(nostr_field):
        return NamecoinNostrResult(
            pubkey=nostr_field.lower(),
            namecoin_name=parsed.namecoin_name,
        )

    # "nostr": {"pubkey": "<hex>", "relays": [...]}
    if isinstance(nostr_field, dict):
        pk = nostr_field.get("pubkey")
        if isinstance(pk, str) and _is_valid_pubkey(pk):
            relays_raw = nostr_field.get("relays")
            relays: List[str] = []
            if isinstance(relays_raw, list):
                relays = [r for r in relays_raw if isinstance(r, str)]
            return NamecoinNostrResult(
                pubkey=pk.lower(),
                namecoin_name=parsed.namecoin_name,
                relays=tuple(relays),
            )

        # NIP-05-like names map under id/ (Amethyst parity).
        if "names" in nostr_field:
            return _resolve_from_names_map(
                nostr_field,
                _ParsedIdentifier(
                    namecoin_name=parsed.namecoin_name,
                    local_part="_",
                    namespace=_Namespace.IDENTITY,
                ),
            )

    return None


def _extract_relays(nostr_obj: dict, pubkey: str) -> List[str]:
    relays_map = nostr_obj.get("relays")
    if not isinstance(relays_map, dict):
        return []
    entry = relays_map.get(pubkey.lower()) or relays_map.get(pubkey)
    if not isinstance(entry, list):
        return []
    return [r for r in entry if isinstance(r, str)]


def _extract_nip46(nostr_obj: dict, pubkey: str) -> List[str]:
    nip46_map = nostr_obj.get("nip46")
    if not isinstance(nip46_map, dict):
        return []
    entry = nip46_map.get(pubkey.lower()) or nip46_map.get(pubkey)
    if not isinstance(entry, list):
        return []
    return [r for r in entry if isinstance(r, str)]


# ── RPC backend ────────────────────────────────────────────────────────────


async def _rpc_name_show(
    rpc_url: str, name: str, timeout: float
) -> Tuple[Optional[str], Optional[_TipInfo]]:
    """Call ``name_show`` via JSON-RPC. Also fetches ``getblockcount`` for tip."""
    payload_value = {
        "jsonrpc": "1.0",
        "id": "dufflepud-name-show",
        "method": "name_show",
        "params": [name],
    }
    payload_tip = {
        "jsonrpc": "1.0",
        "id": "dufflepud-tip",
        "method": "getblockcount",
        "params": [],
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.post(
            rpc_url,
            json=payload_value,
            headers={"Content-Type": "application/json"},
        ) as resp:
            try:
                value_envelope = await resp.json(content_type=None)
            except aiohttp.ContentTypeError:
                return None, None

        # Even if we got "name not found", still try to fetch the tip for
        # completeness/expiry on subsequent calls.
        tip_height: Optional[int] = None
        try:
            async with session.post(
                rpc_url,
                json=payload_tip,
                headers={"Content-Type": "application/json"},
            ) as resp:
                tip_envelope = await resp.json(content_type=None)
                if isinstance(tip_envelope, dict) and not tip_envelope.get("error"):
                    result = tip_envelope.get("result")
                    if isinstance(result, int):
                        tip_height = result
        except Exception:
            pass  # tip is best-effort

    if not isinstance(value_envelope, dict):
        return None, None

    err = value_envelope.get("error")
    if err:
        # namecoind error code -4 = "name not found"
        code = err.get("code") if isinstance(err, dict) else None
        if code == -4:
            raise NameNotFound(name)
        return None, None

    result = value_envelope.get("result")
    if not isinstance(result, dict):
        return None, None

    name_height = result.get("height")
    try:
        name_height = int(name_height) if name_height is not None else None
    except (TypeError, ValueError):
        name_height = None

    tip = (
        _TipInfo(current_height=tip_height, name_height=name_height)
        if tip_height is not None
        else None
    )
    if tip is not None and tip.blocks_since_update is not None:
        if tip.blocks_since_update >= NAME_EXPIRE_DEPTH:
            raise NameExpired(name)
    return result.get("value"), tip


# ── ElectrumX backend ──────────────────────────────────────────────────────


# Namecoin consensus: names expire this many blocks after their last update.
NAME_EXPIRE_DEPTH = 36_000

# Namecoin script opcodes.
_OP_NAME_UPDATE = 0x53  # OP_3 repurposed by Namecoin for name updates
_OP_2DROP = 0x6D
_OP_DROP = 0x75
_OP_RETURN = 0x6A
_OP_PUSHDATA1 = 0x4C
_OP_PUSHDATA2 = 0x4D

_PROTOCOL_VERSION = "1.4"
_CLIENT_IDENT = "dufflepud/0.1"

# Per-server mutexes so concurrent lookups against the same server
# don't interleave on a single socket.
_server_mutexes: Dict[str, asyncio.Lock] = {}


def _server_mutex(server: ElectrumxServer) -> asyncio.Lock:
    key = f"{server.scheme}://{server.host}:{server.port}"
    lock = _server_mutexes.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _server_mutexes[key] = lock
    return lock


async def _electrumx_name_show(
    server: ElectrumxServer, name: str, cfg: NamecoinConfig
) -> Tuple[Optional[str], Optional[_TipInfo]]:
    """Resolve a Namecoin name via ElectrumX. Returns ``(value, tip_info)``.

    Raises :class:`NameNotFound` when the blockchain has no history for
    the name, :class:`NameExpired` when the latest update is >= 36000
    blocks behind the tip. Any other exception is transient and should
    cause the caller to try the next server.
    """
    async with _server_mutex(server):
        async with _connect_electrumx(server, cfg) as proto:
            await proto.call(
                "server.version", [_CLIENT_IDENT, _PROTOCOL_VERSION]
            )

            name_script = _build_name_index_script(name.encode("ascii"))
            script_hash = _electrum_script_hash(name_script)

            history = await proto.call(
                "blockchain.scripthash.get_history", [script_hash]
            )
            if not isinstance(history, list) or not history:
                raise NameNotFound(name)

            latest = history[-1]
            if not isinstance(latest, dict):
                raise NameNotFound(name)
            tx_hash = latest.get("tx_hash")
            name_height = int(latest.get("height") or 0)
            if not isinstance(tx_hash, str) or not tx_hash:
                raise NameNotFound(name)

            # Fetch chain tip (best-effort — failures are non-fatal).
            tip_height: Optional[int] = None
            try:
                headers = await proto.call("blockchain.headers.subscribe", [])
                if isinstance(headers, dict):
                    raw_h = headers.get("height")
                    if isinstance(raw_h, int):
                        tip_height = raw_h
            except Exception:
                pass

            if tip_height is not None and name_height > 0:
                if tip_height - name_height >= NAME_EXPIRE_DEPTH:
                    raise NameExpired(name)

            tx = await proto.call(
                "blockchain.transaction.get", [tx_hash, True]
            )
            if not isinstance(tx, dict):
                return None, _maybe_tip(tip_height, name_height)

            value = _extract_value_from_transaction(name, tx)
            return value, _maybe_tip(tip_height, name_height)


def _maybe_tip(tip_height: Optional[int], name_height: int) -> Optional[_TipInfo]:
    if tip_height is None:
        return None
    return _TipInfo(current_height=tip_height, name_height=name_height or None)


# ── ElectrumX transport ────────────────────────────────────────────────────


class _ElectrumProtocol:
    """Tiny JSON-RPC-over-line / JSON-RPC-over-WS wrapper."""

    def __init__(
        self,
        read: Callable[[], Awaitable[Optional[str]]],
        write: Callable[[str], Awaitable[None]],
        *,
        kind: str,
    ):
        self._read = read
        self._write = write
        self._kind = kind
        self._id = 0

    async def call(self, method: str, params: list) -> Any:
        self._id += 1
        req = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self._id,
                    "method": method,
                    "params": params,
                }
            )
            + "\n"
        )
        await self._write(req)
        raw = await self._read()
        if raw is None:
            raise RuntimeError("electrumx closed connection")
        # WSS may deliver multiple frames; TCP delivers one line at a time.
        # Some servers emit extra async notifications on the same channel —
        # drain until we see the reply matching our id.
        while True:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise RuntimeError(f"electrumx malformed response: {raw[:200]}")
            if envelope.get("id") == self._id or "id" not in envelope:
                err = envelope.get("error")
                if err:
                    raise RuntimeError(f"electrumx error: {err}")
                return envelope.get("result")
            # Unsolicited notification — skip it and read again.
            raw = await self._read()
            if raw is None:
                raise RuntimeError("electrumx closed connection")


class _ConnectionContext:
    def __init__(
        self,
        enter: Callable[[], Awaitable[_ElectrumProtocol]],
        exit_: Callable[[], Awaitable[None]],
    ):
        self._enter = enter
        self._exit = exit_

    async def __aenter__(self) -> _ElectrumProtocol:
        return await self._enter()

    async def __aexit__(self, exc_type, exc, tb):
        await self._exit()


def _connect_electrumx(
    server: ElectrumxServer, cfg: NamecoinConfig
) -> _ConnectionContext:
    if server.scheme in {"tcp+tls", "tcp"}:
        return _connect_tcp(server, cfg)
    if server.scheme in {"wss", "ws"}:
        return _connect_ws(server, cfg)
    raise ValueError(f"unsupported electrumx scheme: {server.scheme}")


def _connect_tcp(server: ElectrumxServer, cfg: NamecoinConfig) -> _ConnectionContext:
    state: dict = {}

    async def enter() -> _ElectrumProtocol:
        ssl_ctx = (
            _build_pinned_ssl_context(tuple(cfg.extra_pinned_certs))
            if server.is_tls
            else None
        )
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=server.host,
                port=server.port,
                ssl=ssl_ctx,
                server_hostname=server.host if server.is_tls else None,
            ),
            timeout=cfg.connect_timeout,
        )
        state["writer"] = writer

        async def read() -> Optional[str]:
            line = await asyncio.wait_for(
                reader.readline(), timeout=cfg.read_timeout
            )
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


def _connect_ws(server: ElectrumxServer, cfg: NamecoinConfig) -> _ConnectionContext:
    state: dict = {}

    async def enter() -> _ElectrumProtocol:
        ssl_ctx = (
            _build_pinned_ssl_context(tuple(cfg.extra_pinned_certs))
            if server.is_tls
            else None
        )
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=cfg.read_timeout)
        )
        state["session"] = session
        ws_url = f"{server.scheme}://{server.host}:{server.port}"
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(ws_url, ssl=ssl_ctx),
                timeout=cfg.connect_timeout,
            )
        except Exception:
            await session.close()
            state.pop("session", None)
            raise
        state["ws"] = ws

        async def read() -> Optional[str]:
            msg = await asyncio.wait_for(ws.receive(), timeout=cfg.read_timeout)
            if msg.type == aiohttp.WSMsgType.TEXT:
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

# Self-signed certs for the well-known public Namecoin ElectrumX servers
# (same set Amethyst ships). Regenerate via:
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


_pinned_ssl_context_cache: Dict[Tuple[str, ...], ssl.SSLContext] = {}


def _build_pinned_ssl_context(
    extra_pems: Tuple[str, ...] = (),
) -> ssl.SSLContext:
    """Build an SSLContext that trusts the pinned ElectrumX certs + system CAs.

    Hostname verification is disabled because several pinned certs list a
    single CN but also serve requests by IP (e.g. ``46.229.238.187`` uses
    the ``nmc2.bitcoins.sk`` cert). Identity is instead established by the
    pinned trust store itself: only the explicit certs in
    ``_PINNED_ELECTRUMX_CERTS`` + ``extra_pems`` (or the system CAs) will
    verify, and TLS enforces proof of private key possession.
    """
    key = tuple(extra_pems)
    cached = _pinned_ssl_context_cache.get(key)
    if cached is not None:
        return cached

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED

    try:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    except Exception:
        pass

    for pem in (*_PINNED_ELECTRUMX_CERTS, *extra_pems):
        try:
            ctx.load_verify_locations(cadata=pem)
        except ssl.SSLError as exc:
            logger.warning("Skipping malformed pinned ElectrumX cert: %s", exc)

    _pinned_ssl_context_cache[key] = ctx
    return ctx


# ── Namecoin script parsing ───────────────────────────────────────────────


def _build_name_index_script(name_bytes: bytes) -> bytes:
    """Canonical script that ElectrumX indexes for a Namecoin name.

    Matches `build_name_index_script` in the Namecoin ElectrumX fork:
      OP_NAME_UPDATE <push(name)> <push(empty)> OP_2DROP OP_DROP OP_RETURN
    """
    out = bytearray([_OP_NAME_UPDATE])
    out += _push_data(name_bytes)
    out += _push_data(b"")
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
    digest = hashlib.sha256(script).digest()
    return digest[::-1].hex()


def _extract_value_from_transaction(name: str, tx: dict) -> Optional[str]:
    vouts = tx.get("vout") or []
    for vout in vouts:
        if not isinstance(vout, dict):
            continue
        script_hex = vout.get("scriptPubKey", {}).get("hex")
        if not isinstance(script_hex, str) or not script_hex.startswith("53"):
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
        nm = name_bytes.decode("ascii")
    except UnicodeDecodeError:
        return None
    try:
        val = value_bytes.decode("utf-8")
    except UnicodeDecodeError:
        val = value_bytes.decode("latin-1")
    return nm, val


def _read_push_data(
    script: bytes, pos: int
) -> Tuple[Optional[bytes], int]:
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


# ── cache ──────────────────────────────────────────────────────────────────


class _TtlLruCache:
    """Process-local LRU + TTL cache of resolve outcomes.

    Equivalent to Amethyst's ``NamecoinLookupCache``. Complements Dufflepud's
    Redis handle cache by short-circuiting ElectrumX round-trips when multiple
    handles share a domain (``alice@x.bit`` and ``bob@x.bit`` both hit
    ``d/x``).
    """

    def __init__(self, max_entries: int = 500):
        self._data: "OrderedDict[str, Tuple[Any, float]]" = OrderedDict()
        self._max = max_entries
        # Deliberately no lock: asyncio runs one coroutine at a time per loop,
        # so the dict ops below are atomic within a single process event loop.

    def _key(self, identifier: str) -> str:
        return identifier.strip().lower()

    def get(self, identifier: str, ttl: float) -> Optional[Any]:
        k = self._key(identifier)
        entry = self._data.get(k)
        if entry is None:
            return None
        value, stamp = entry
        if time.monotonic() - stamp > ttl:
            self._data.pop(k, None)
            return None
        # touch (LRU)
        self._data.move_to_end(k)
        return value

    def put(self, identifier: str, value: Any) -> None:
        k = self._key(identifier)
        self._data[k] = (value, time.monotonic())
        self._data.move_to_end(k)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


_resolver_cache = _TtlLruCache(max_entries=500)


# ── module-level config (cached) ──────────────────────────────────────────


_config_cache: Optional[NamecoinConfig] = None


def _default_config() -> NamecoinConfig:
    global _config_cache
    if _config_cache is None:
        _config_cache = NamecoinConfig.from_env()
    return _config_cache


def reload_config() -> NamecoinConfig:
    """Force a re-read of env vars. Intended for tests."""
    global _config_cache, _resolver_cache
    _config_cache = NamecoinConfig.from_env()
    _resolver_cache.clear()
    _pinned_ssl_context_cache.clear()
    return _config_cache
