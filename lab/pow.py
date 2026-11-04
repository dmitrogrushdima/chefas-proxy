"""Bitcoin SHA-256d reference helpers for the test lab, not a pool implementation."""
import hashlib


def hash_header(header):
    if len(header) != 80:
        raise ValueError("Bitcoin block header must be 80 bytes")
    return hashlib.sha256(hashlib.sha256(header).digest()).digest()


def compact_target(bits):
    """Decode Bitcoin compact target; reject negative, zero and overflow values."""
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
