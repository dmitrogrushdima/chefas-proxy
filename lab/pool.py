"""Local-only toy Stratum pool for deterministic SHA-256d integration tests.

This is deliberately incomplete and is not a real mining pool.
"""
import asyncio
import hashlib
import json
import secrets

from lab.pow import meets_target, compact_target

SHARE_TARGET = (1 << 248) - 1
JOB_ID = "lab-job"
VERSION = "20000000"
PREVHASH = "00" * 32
COINB1 = "01020304"
COINB2 = "05060708"
NTIME = "65000000"
NBITS = "1d00ffff"


def double_sha(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def build_header(extranonce1, extranonce2, nonce):
    """Build the lab's job header; fields are fixed-width hex strings."""
    if len(extranonce1) != 8 or len(extranonce2) != 8 or len(nonce) != 8:
        raise ValueError("invalid field length")
    coinbase = bytes.fromhex(COINB1 + extranonce1 + extranonce2 + COINB2)
    return (bytes.fromhex(VERSION)[::-1] + bytes.fromhex(PREVHASH) + double_sha(coinbase) +
            bytes.fromhex(NTIME)[::-1] + bytes.fromhex(NBITS)[::-1] + bytes.fromhex(nonce)[::-1])


def find_nonce(extranonce1, extranonce2="00000000", start=0):
    for number in range(start, min(start + 100000, 2 ** 32)):
        nonce = f"{number:08x}"
        if meets_target(build_header(extranonce1, extranonce2, nonce), SHARE_TARGET):
            return nonce
    raise RuntimeError("no share in lab search range")


class LabPool:
    def __init__(self):
        self.accepted = 0
        self.rejected = 0

    async def handle(self, reader, writer):
        extranonce1 = secrets.token_hex(4)
        subscribed = False
        authorized = set()
        seen = set()

        async def send(value):
            writer.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
            await writer.drain()

        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                    ident, method = req.get("id"), req.get("method")
                    params = req.get("params", [])
                    if not isinstance(params, list):
                        raise ValueError("invalid params")
                    if method == "mining.subscribe":
                        subscribed = True
                        await send({"id": ident, "result": [[['mining.notify', 'lab']], extranonce1, 4], "error": None})
                    elif method == "mining.authorize" and subscribed and len(params) >= 1 and isinstance(params[0], str):
                        authorized.add(params[0])
                        await send({"id": ident, "result": True, "error": None})
                        await send({"id": None, "method": "mining.set_difficulty", "params": [compact_target(0x1d00ffff) / SHARE_TARGET]})
                        await send({"id": None, "method": "mining.notify", "params": [JOB_ID, PREVHASH, COINB1, COINB2, [], VERSION, NBITS, NTIME, True]})
                    elif method == "mining.submit" and len(params) == 5:
                        worker, job, extra, ntime, nonce = params
                        key = (extra, nonce)
                        valid = (worker in authorized and job == JOB_ID and ntime == NTIME and
                                 all(isinstance(v, str) for v in (extra, nonce)) and key not in seen and
                                 meets_target(build_header(extranonce1, extra, nonce), SHARE_TARGET))
                        if valid:
                            seen.add(key)
                            self.accepted += 1
                        else:
                            self.rejected += 1
                        await send({"id": ident, "result": valid, "error": None if valid else [23, "invalid share", None]})
                    else:
                        await send({"id": ident, "result": None, "error": [20, "unsupported request", None]})
                except (ValueError, TypeError, AttributeError, IndexError):
                    self.rejected += 1
                    await send({"id": None, "result": None, "error": [20, "invalid request", None]})
        except (OSError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
