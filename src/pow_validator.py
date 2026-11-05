"""Bitcoin Stratum V1 header reconstruction and SHA-256d share validation.

The caller must bind a job to its issuing connection's extranonce, negotiated
version mask and difficulty. Authorization, job expiry, duplicate detection and
pool-specific time limits belong to the session router, not this pure module.
"""
import hashlib
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction


MAX_TARGET = (1 << 256) - 1
# Bitcoin/Stratum difficulty one (bdiff), not the rounded pdiff approximation.
DIFF_ONE_TARGET = 0xffff << 208
_HEX = re.compile(r"[0-9a-fA-F]*\Z")


def _hex_bytes(value, name, *, size=None, max_size=32768):
    """Decode strict protocol hex; bytes.fromhex alone accepts whitespace."""
    if (not isinstance(value, str) or len(value) % 2 or
            len(value) > max_size * 2 or not _HEX.fullmatch(value) or
            (size is not None and len(value) != size * 2)):
        raise ValueError(f"invalid {name}")
    return bytes.fromhex(value)


def _double_sha(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash_header(header):
    if len(header) != 80:
        raise ValueError("Bitcoin block header must be 80 bytes")
    return _double_sha(header)


def compact_target(bits):
    if not 0 <= bits <= 0xffffffff:
        raise ValueError("invalid compact value")
    size = bits >> 24
    word = bits & 0x007fffff
    target = word >> (8 * (3 - size)) if size <= 3 else word << (8 * (size - 3))
    if bits & 0x00800000 or target == 0 or target.bit_length() > 256:
        raise ValueError("invalid target")
    return target


def meets_target(header, target):
    if not 0 < target < 2 ** 256:
        raise ValueError("invalid target")
    return int.from_bytes(hash_header(header), "little") <= target


def target_from_difficulty(difficulty):
    """Return floor(diff1 / difficulty) using exact rational arithmetic.

    Decimal values parsed directly from JSON are preferred. Floats are read as
    their decimal spelling, so callers can avoid an extra binary approximation.
    Very small valid difficulties are capped at the largest 256-bit target.
    """
    if isinstance(difficulty, bool):
        raise ValueError("invalid difficulty")
    try:
        if isinstance(difficulty, Fraction):
            value = difficulty
        else:
            spelling = str(difficulty)
            if len(spelling) > 1024:
                raise ValueError("invalid difficulty")
            decimal = Decimal(spelling)
            if not decimal.is_finite() or not -256 <= decimal.adjusted() <= 256:
                raise ValueError("invalid difficulty")
            value = Fraction(decimal)
        if (value <= 0 or value.numerator.bit_length() > 4096 or
                value.denominator.bit_length() > 4096):
            raise ValueError("invalid difficulty")
        target = DIFF_ONE_TARGET * value.denominator // value.numerator
    except (TypeError, InvalidOperation, OverflowError, ZeroDivisionError) as exc:
        raise ValueError("invalid difficulty") from exc
    if target < 1:
        raise ValueError("difficulty produces a zero target")
    return min(target, MAX_TARGET)


def build_stratum_header(job_params, extranonce1, extranonce2, ntime, nonce,
                         version_bits=None, version_mask=None):
    """Reconstruct the 80-byte Bitcoin header from a V1 notify and submit.

    Stratum V1 prevhash uses big-endian 32-bit words in internal word order;
    each word is byte-swapped for the serialized Bitcoin header. Coinbase and
    merkle-branch digests use their raw byte order. BIP310 version bits replace
    the negotiated bits, rather than being ORed into the template version.
    """
    if not isinstance(job_params, (list, tuple)) or len(job_params) != 9:
        raise ValueError("invalid mining.notify parameters")
    _, prevhash, coinb1, coinb2, branches, version, nbits, _, clean = job_params
    if not isinstance(branches, (list, tuple)) or len(branches) > 32:
        raise ValueError("invalid merkle branches")
    if not isinstance(clean, bool):
        raise ValueError("invalid clean_jobs flag")
    # Validate the template time too, although rolled ntime is used below.
    _hex_bytes(job_params[7], "job ntime", size=4)
    previous = _hex_bytes(prevhash, "prevhash", size=32)
    prefix = _hex_bytes(coinb1, "coinbase prefix")
    suffix = _hex_bytes(coinb2, "coinbase suffix")
    extra1 = _hex_bytes(extranonce1, "extranonce1", max_size=256)
    extra2 = _hex_bytes(extranonce2, "extranonce2", max_size=256)
    version_number = int.from_bytes(_hex_bytes(version, "version", size=4), "big")
    if version_bits is not None:
        if version_mask is None:
            raise ValueError("version rolling was not negotiated")
        mask = int.from_bytes(_hex_bytes(version_mask, "version mask", size=4), "big")
        bits = int.from_bytes(_hex_bytes(version_bits, "version bits", size=4), "big")
        if bits & ~mask:
            raise ValueError("version bits exceed negotiated mask")
        version_number = (version_number & ~mask) | bits
    elif version_mask is not None:
        # A negotiated BIP310 session requires the extra submit parameter.
        _hex_bytes(version_mask, "version mask", size=4)
        raise ValueError("missing version bits")
    root = _double_sha(prefix + extra1 + extra2 + suffix)
    for branch in branches:
        root = _double_sha(root + _hex_bytes(branch, "merkle branch", size=32))
    previous = b"".join(previous[offset:offset + 4][::-1]
                        for offset in range(0, 32, 4))
    return (version_number.to_bytes(4, "little") + previous + root +
            _hex_bytes(ntime, "ntime", size=4)[::-1] +
            _hex_bytes(nbits, "nbits", size=4)[::-1] +
            _hex_bytes(nonce, "nonce", size=4)[::-1])


def validate_stratum_share(job_params, extranonce1, extranonce2, ntime, nonce,
                           version_bits=None, version_mask=None, difficulty=1,
                           *, extranonce2_size=None):
    """Return whether the reconstructed share meets its captured job difficulty.

    Malformed fields raise ValueError; a well-formed low-difficulty share returns
    False. This validates SHA-256d only, not Bitcoin block/transaction consensus.
    """
    if extranonce2_size is not None:
        if (isinstance(extranonce2_size, bool) or
                not isinstance(extranonce2_size, int) or
                not 0 <= extranonce2_size <= 256):
            raise ValueError("invalid extranonce2 size")
        _hex_bytes(extranonce2, "extranonce2", size=extranonce2_size, max_size=256)
    header = build_stratum_header(job_params, extranonce1, extranonce2, ntime,
                                  nonce, version_bits, version_mask)
    return meets_target(header, target_from_difficulty(difficulty))
