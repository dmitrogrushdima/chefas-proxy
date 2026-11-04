"""Low-level SHA-256d helpers, not a complete Stratum share validator.

These functions do not reconstruct jobs, bind difficulty, detect duplicates,
or validate version-rolling. They must not be used to filter live shares yet.
"""
import hashlib


def hash_header(header):
    if len(header) != 80:
        raise ValueError("Bitcoin block header must be 80 bytes")
    return hashlib.sha256(hashlib.sha256(header).digest()).digest()


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
