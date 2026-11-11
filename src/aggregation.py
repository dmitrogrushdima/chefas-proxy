"""Pure, bounded primitives for a future shared Stratum V1 upstream.

This module does not consolidate sockets. A router using it must still bind
leases and job snapshots to one upstream connection, negotiate compatible ASIC
capabilities, isolate accounts, expire jobs, reject duplicates and distinguish
local share acknowledgements from pool-confirmed accepted difficulty.

Low-difficulty proofs cannot be added together to create a high-difficulty
proof. Each header is hashed once and independently checked against both
targets; a network block candidate must always be forwarded.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from collections import OrderedDict
import re
import time
from typing import Optional

from pow_validator import (MAX_TARGET, build_stratum_header, compact_target,
                           hash_header, target_from_difficulty)


_HEX = re.compile(r"[0-9a-fA-F]*\Z")


def _hex(value: str, name: str, *, size: Optional[int] = None,
         maximum: int = 256) -> str:
    if (not isinstance(value, str) or len(value) % 2 or
            len(value) > maximum * 2 or not _HEX.fullmatch(value) or
            (size is not None and len(value) != size * 2)):
        raise ValueError("invalid " + name)
    return value.lower()


def _size(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 32:
        raise ValueError("invalid " + name)
    return value


def _workers(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 10000:
        raise ValueError("maximum_workers must be 1..10000")
    return value


def _number(value, *, positive: bool = True) -> Fraction:
    if isinstance(value, bool):
        raise ValueError("invalid number")
    try:
        if isinstance(value, Fraction):
            number = value
        else:
            spelling = str(value)
            if len(spelling) > 256:
                raise ValueError("invalid number")
            decimal = Decimal(spelling)
            if not decimal.is_finite() or not -256 <= decimal.adjusted() <= 256:
                raise ValueError("invalid number")
            number = Fraction(decimal)
    except (InvalidOperation, OverflowError, TypeError, ZeroDivisionError) as exc:
        raise ValueError("invalid number") from exc
    if ((number <= 0 if positive else number < 0) or
            number.numerator.bit_length() > 4096 or
            number.denominator.bit_length() > 4096):
        raise ValueError("invalid number")
    return number


def _bounded_estimate(value: Fraction) -> Fraction:
    # A repeated exact rational EMA grows its denominator on every sample.
    # Rate estimates are statistical, unlike the exact accepted-work ledger.
    # Round the estimator to 28 significant decimal digits to bound its state.
    with localcontext() as context:
        context.prec = 28
        return Fraction(Decimal(value.numerator) / Decimal(value.denominator))


@dataclass(frozen=True)
class AggregationKey:
    """Only equal keys may share an upstream; password is never in repr.

    ``worker`` is the explicitly configured common pool account/worker, not an
    arbitrary ASIC wallet. ``tls_policy`` identifies the trust configuration;
    equal host names with different CA policies are deliberately separate.
    The exact ASIC agent and negotiated version mask are also group boundaries.
    """

    host: str
    port: int
    use_tls: bool
    worker: str
    password: str = field(repr=False)
    version_mask: Optional[str] = None
    user_agent: str = ""
    tls_policy: str = "default"
    tenant: str = ""

    def __post_init__(self):
        for name in ("host", "worker", "password", "user_agent", "tls_policy"):
            value = getattr(self, name)
            if (not isinstance(value, str) or len(value) > 4096 or
                    any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError("invalid " + name)
        if (not self.host or any(char.isspace() for char in self.host) or
                not self.worker or not self.tls_policy):
            raise ValueError("missing group identity")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("invalid port")
        if type(self.use_tls) is not bool:
            raise ValueError("invalid TLS flag")
        object.__setattr__(self, "host", self.host.lower())
        if self.version_mask is not None:
            object.__setattr__(self, "version_mask",
                               _hex(self.version_mask, "version mask", size=4))


class InsufficientNonceSpace(ValueError):
    """Use another upstream or 1:1 routing; never overlap ASIC search spaces."""


class ExtranonceSizeCache:
    """Short-lived hints, never a substitute for validating the live pool reply."""

    def __init__(self, *, capacity=256, ttl=300, clock=time.monotonic):
        if type(capacity) is not int or capacity < 1 or ttl <= 0 or not callable(clock):
            raise ValueError('invalid extranonce cache configuration')
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._entries = OrderedDict()

    def remember(self, key, upstream_size):
        _size(upstream_size, 'upstream extranonce2 size')
        self._entries.pop(key, None)
        self._entries[key] = (upstream_size, self.clock())
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def get(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self.clock() - entry[1] >= self.ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry[0]


class VirtualPrefixAllocator:
    """Allocate provisional subscription prefixes without reuse in one epoch.

    Keep the allocator for the lifetime of every upstream that might receive
    these prefixes, including any retired connection with jobs in flight.
    A new epoch must not share a live upstream with an old epoch.
    """

    def __init__(self, prefix_bytes, local_extranonce2_size, *, maximum_workers=100):
        if type(prefix_bytes) is not int or not 1 <= prefix_bytes <= 4:
            raise ValueError('prefix_bytes must be 1..4')
        self.prefix_bytes = prefix_bytes
        self.local_extranonce2_size = _size(local_extranonce2_size, 'local extranonce2 size')
        self.maximum_workers = _workers(maximum_workers)
        self._next = 0
        self._active = {}

    def allocate(self, session_id):
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError('invalid session id')
        if session_id in self._active:
            return self._active[session_id]
        if len(self._active) >= self.maximum_workers or self._next >= 1 << (8 * self.prefix_bytes):
            raise InsufficientNonceSpace('virtual prefix space exhausted')
        prefix = self._next.to_bytes(self.prefix_bytes, 'big').hex()
        self._next += 1
        self._active[session_id] = prefix
        return prefix

    def release(self, session_id):
        return self._active.pop(session_id, None) is not None

    def reset_if_idle(self):
        """Call only when every upstream from this epoch has been retired."""
        if not self._active:
            self._next = 0

    def compatible(self, upstream_extranonce2_size):
        _size(upstream_extranonce2_size, 'upstream extranonce2 size')
        return self.prefix_bytes + self.local_extranonce2_size <= upstream_extranonce2_size


def virtual_subscription_job(params, upstream_extranonce1, upstream_extranonce2_size,
                             virtual_extranonce1, local_extranonce2_size):
    """Translate a pool job for a locally assigned subscription.

    The virtual extranonce is a prefix of the pool's extranonce2, so the
    miner's coinbase must contain the pool extranonce1 in coinbase1. This
    function does not allocate unique prefixes or negotiate ASIC capabilities.
    """
    if not isinstance(params, (list, tuple)) or len(params) != 9:
        raise ValueError('invalid mining.notify parameters')
    upstream = _hex(upstream_extranonce1, 'upstream extranonce1')
    prefix = _hex(virtual_extranonce1, 'virtual extranonce1')
    _size(upstream_extranonce2_size, 'upstream extranonce2 size')
    _size(local_extranonce2_size, 'local extranonce2 size')
    padding = upstream_extranonce2_size - len(prefix) // 2 - local_extranonce2_size
    if padding < 0:
        raise InsufficientNonceSpace('virtual subscription does not fit upstream nonce space')
    coinbase1 = _hex(params[2], 'coinbase prefix', maximum=32768)
    if len(coinbase1 + upstream) > 65536:
        raise ValueError('coinbase prefix too large')
    result = list(params)
    result[2] = coinbase1 + upstream
    coinbase2 = _hex(params[3], 'coinbase suffix', maximum=32768)
    result[3] = '00' * padding + coinbase2
    if isinstance(result[4], (list, tuple)):
        result[4] = list(result[4])
    return result


def choose_local_extranonce2_size(upstream_size: int, *, minimum_local_size: int,
                                 preferred_local_size: int,
                                 maximum_workers: int = 100) -> int:
    """Reserve fixed prefix bytes only within explicitly allowed ASIC bounds.

    For example an upstream width of four and an ASIC minimum of four cannot
    serve 100 ASICs. A minimum of three permits one prefix byte and three local
    bytes. This is a capability decision, not a transparent universal rewrite.
    """
    _size(upstream_size, "upstream extranonce2 size")
    _size(minimum_local_size, "minimum local extranonce2 size")
    _size(preferred_local_size, "preferred local extranonce2 size")
    _workers(maximum_workers)
    if minimum_local_size > preferred_local_size:
        raise ValueError("minimum exceeds preferred extranonce2 size")
    reserved = ((maximum_workers - 1).bit_length() + 7) // 8
    local = min(preferred_local_size, upstream_size - reserved)
    if local < minimum_local_size:
        raise InsufficientNonceSpace("insufficient extranonce2 bytes for group")
    return local


@dataclass(frozen=True)
class NonceLease:
    """Immutable search-space assignment, retained by each in-flight job."""

    worker_id: str
    upstream_extranonce1: str
    upstream_extranonce2_size: int
    prefix: str
    local_extranonce2_size: int

    def __post_init__(self):
        if (not isinstance(self.worker_id, str) or not self.worker_id or
                len(self.worker_id) > 256):
            raise ValueError("invalid worker id")
        _size(self.upstream_extranonce2_size, "upstream extranonce2 size")
        _size(self.local_extranonce2_size, "local extranonce2 size")
        prefix_size = self.upstream_extranonce2_size - self.local_extranonce2_size
        if prefix_size < 0:
            raise ValueError("local extranonce2 exceeds upstream width")
        object.__setattr__(self, "upstream_extranonce1",
                           _hex(self.upstream_extranonce1, "extranonce1"))
        object.__setattr__(self, "prefix", _hex(self.prefix, "prefix", size=prefix_size))
        if len(self.upstream_extranonce1 + self.prefix) > 512:
            raise ValueError("downstream extranonce1 exceeds validator bound")

    @property
    def downstream_extranonce1(self) -> str:
        return self.upstream_extranonce1 + self.prefix

    def upstream_extranonce2(self, local_extranonce2: str) -> str:
        """Preserve coinbase bytes exactly when translating a valid submit."""
        return self.prefix + _hex(local_extranonce2, "local extranonce2",
                                  size=self.local_extranonce2_size)


class NoncePartitioner:
    """Fixed-width, never-reused prefixes for exactly one upstream lifetime.

    Active leases are capped by configuration. Releasing a worker frees an active slot but
    does not recycle its prefix, since old jobs may still be in flight. Exhausted
    prefix space requires a fresh upstream session before any new allocation.
    Instances must not be reset/recreated while reusing the same pool session.
    """

    def __init__(self, upstream_extranonce1: str, upstream_extranonce2_size: int,
                 local_extranonce2_size: int, *, maximum_workers: int = 100):
        self.upstream_extranonce1 = _hex(upstream_extranonce1, "extranonce1")
        self.upstream_extranonce2_size = _size(upstream_extranonce2_size, "upstream extranonce2 size")
        self.local_extranonce2_size = _size(local_extranonce2_size, "local extranonce2 size")
        self.maximum_workers = _workers(maximum_workers)
        self._prefix_size = upstream_extranonce2_size - local_extranonce2_size
        if self._prefix_size < 0:
            raise InsufficientNonceSpace("local extranonce2 exceeds upstream width")
        self._capacity = 1 << (self._prefix_size * 8)
        if self._capacity < maximum_workers:
            raise InsufficientNonceSpace("not enough distinct prefixes for group")
        if len(self.upstream_extranonce1) + self._prefix_size * 2 > 512:
            raise ValueError("downstream extranonce1 exceeds validator bound")
        self._next_prefix = 0
        self._active: dict[str, NonceLease] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def remaining_prefixes(self) -> int:
        return self._capacity - self._next_prefix

    def allocate(self, worker_id: str) -> NonceLease:
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 256:
            raise ValueError("invalid worker id")
        if worker_id in self._active:
            return self._active[worker_id]
        if len(self._active) >= self.maximum_workers:
            raise InsufficientNonceSpace("group active worker limit reached")
        if self._next_prefix >= self._capacity:
            raise InsufficientNonceSpace("prefixes exhausted; fresh upstream required")
        prefix = self._next_prefix.to_bytes(self._prefix_size, "big").hex()
        lease = NonceLease(worker_id, self.upstream_extranonce1,
                           self.upstream_extranonce2_size, prefix,
                           self.local_extranonce2_size)
        self._next_prefix += 1
        self._active[worker_id] = lease
        return lease

    def release(self, worker_id: str) -> bool:
        return self._active.pop(worker_id, None) is not None


class LocalVardiff:
    """Share-rate estimator whose difficulty changes only on ``new_job``.

    Feed only unique, locally valid shares and their captured job difficulty.
    An EMA of seconds per difficulty unit estimates the next target interval.
    Adaptive changes are capped at 0.5x..2x per observed job boundary. A real
    pool difficulty decrease takes precedence over that smoothing bound: local
    difficulty must never exceed the new upstream difficulty. This estimator
    neither acknowledges shares nor records pool-confirmed accepted work.
    """

    def __init__(self, initial_difficulty, pool_difficulty, *,
                 minimum_difficulty=1, target_interval=10, alpha=Fraction(1, 4)):
        self.minimum = _number(minimum_difficulty)
        self.target_interval = _number(target_interval)
        self.alpha = _number(alpha)
        if self.alpha > 1:
            raise ValueError("alpha exceeds one")
        self._pool = _number(pool_difficulty)
        self._current = _number(initial_difficulty)
        if not self.minimum <= self._current <= self._pool:
            raise ValueError("difficulty must satisfy minimum <= local <= pool")
        target_from_difficulty(self.minimum)
        target_from_difficulty(self._pool)
        self._last_share: Optional[Fraction] = None
        self._seconds_per_diff: Optional[Fraction] = None
        self._observations = 0
        self._applied_observations = 0

    @property
    def current_difficulty(self) -> Fraction:
        return self._current

    def record_valid_share(self, timestamp, job_difficulty) -> None:
        now = _number(timestamp, positive=False)
        work = _number(job_difficulty)
        target_from_difficulty(work)
        if self._last_share is not None:
            if now < self._last_share:
                raise ValueError("share timestamps must be monotonic")
            if now == self._last_share:
                return  # Clock resolution provides no rate information.
            sample = (now - self._last_share) / work
            self._seconds_per_diff = _bounded_estimate(
                sample if self._seconds_per_diff is None else
                self.alpha * sample + (1 - self.alpha) * self._seconds_per_diff)
            self._observations += 1
        self._last_share = now

    def new_job(self, pool_difficulty=None) -> Fraction:
        """Commit a next-job threshold; older jobs retain their own targets."""
        pool = self._pool if pool_difficulty is None else _number(pool_difficulty)
        target_from_difficulty(pool)
        if pool < self.minimum:
            raise ValueError("pool difficulty is below ASIC minimum")
        desired = self._current
        if (self._seconds_per_diff is not None and
                self._observations != self._applied_observations):
            estimated = self.target_interval / self._seconds_per_diff
            desired = min(self._current * 2,
                          max(self._current / 2, _bounded_estimate(estimated)))
        self._current = min(pool, max(self.minimum, desired))
        self._pool = pool
        self._applied_observations = self._observations
        return self._current


@dataclass(frozen=True)
class ShareTargets:
    """Immutable thresholds attached to a job, never the mutable next vardiff."""

    local: int
    upstream: int
    network: int

    def __post_init__(self):
        if any(type(value) is not int or not 0 < value <= MAX_TARGET
               for value in (self.local, self.upstream, self.network)):
            raise ValueError("invalid share target")
        if self.local < self.upstream:
            raise ValueError("local difficulty must not exceed upstream difficulty")

    @classmethod
    def from_difficulties(cls, local_difficulty, upstream_difficulty,
                          nbits: str) -> "ShareTargets":
        return cls(target_from_difficulty(local_difficulty),
                   target_from_difficulty(upstream_difficulty),
                   compact_target(int(_hex(nbits, "nbits", size=4), 16)))


@dataclass(frozen=True)
class ShareDecision:
    digest: bytes
    local_valid: bool
    upstream_valid: bool
    block_candidate: bool

    @property
    def forward(self) -> bool:
        return self.upstream_valid or self.block_candidate

    @property
    def accept_locally(self) -> bool:
        return self.local_valid or self.block_candidate


def classify_header(header: bytes, targets: ShareTargets) -> ShareDecision:
    """One SHA-256d; each proof stands alone, regardless of other local shares."""
    if not isinstance(targets, ShareTargets):
        raise ValueError("missing captured job targets")
    digest = hash_header(header)
    value = int.from_bytes(digest, "little")
    return ShareDecision(digest, value <= targets.local, value <= targets.upstream,
                         value <= targets.network)


@dataclass(frozen=True)
class AggregationJob:
    """Frozen per-ASIC context; safe to retain across vardiff and lease release.

    A future router must additionally enforce job TTL, clean-job invalidation,
    time limits, authorization and duplicates. ``local_valid`` is not evidence
    of acceptance by a pool and must not be charged to the DevFee ledger.
    """

    params: tuple
    lease: NonceLease
    targets: ShareTargets
    version_mask: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.lease, NonceLease) or not isinstance(self.targets, ShareTargets):
            raise ValueError("invalid aggregation job context")
        if (not isinstance(self.params, (list, tuple)) or len(self.params) != 9 or
                not isinstance(self.params[4], (list, tuple)) or len(self.params[4]) > 32):
            raise ValueError("invalid mining.notify parameters")
        params = tuple(tuple(value) if index == 4 else value
                       for index, value in enumerate(self.params))
        if not all(isinstance(value, str) for index, value in enumerate(params)
                   if index not in (4, 8)) or not all(isinstance(value, str) for value in params[4]):
            raise ValueError("invalid mining.notify fields")
        if not params[0] or len(params[0]) > 4096:
            raise ValueError("invalid job id")
        _hex(params[1], "prevhash", size=32)
        _hex(params[2], "coinbase prefix", maximum=32768)
        _hex(params[3], "coinbase suffix", maximum=32768)
        for branch in params[4]:
            _hex(branch, "merkle branch", size=32)
        _hex(params[5], "version", size=4)
        _hex(params[7], "ntime", size=4)
        if type(params[8]) is not bool:
            raise ValueError("invalid clean_jobs flag")
        if compact_target(int(_hex(params[6], "nbits", size=4), 16)) != self.targets.network:
            raise ValueError("network target does not match job")
        object.__setattr__(self, "params", params)
        if self.version_mask is not None:
            object.__setattr__(self, "version_mask", _hex(self.version_mask, "version mask", size=4))

    def classify_submit(self, extranonce2: str, ntime: str, nonce: str,
                        version_bits: Optional[str] = None) -> ShareDecision:
        self.lease.upstream_extranonce2(extranonce2)  # Strict local width check.
        header = build_stratum_header(self.params, self.lease.downstream_extranonce1,
                                      extranonce2, ntime, nonce,
                                      version_bits, self.version_mask)
        return classify_header(header, self.targets)
