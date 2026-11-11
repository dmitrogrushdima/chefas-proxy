"""One-shot Stratum probe for two explicitly authorized, operator-owned accounts."""

import argparse
import asyncio
from contextlib import suppress
from ipaddress import IPv4Address
import re

from stratum_codec import PacketReader, ProtocolError
from upstream_manager import UpstreamConnection, encode_packet


VIABTC = ("btc.viabtc.io", 3333)


def accepted(reply):
    return reply.get("result") is True and reply.get("error") is None


async def check_template(endpoint, worker, difficulty, timeout=5):
    """Read the pool's first template for this account without submitting work."""
    first_job = asyncio.get_running_loop().create_future()

    async def notify(channel, packet):
        if packet["method"] == "mining.notify" and not first_job.done():
            first_job.set_result(packet["params"])

    async def disconnected(channel):
        if not first_job.done():
            first_job.set_result(None)

    reader, writer = await asyncio.wait_for(asyncio.open_connection(*endpoint), timeout)
    pool = UpstreamConnection(reader, writer, "template-check", notify, disconnected, timeout)
    try:
        subscription = await pool.request("mining.subscribe", ["CHEFAS template check"])
        if not isinstance(subscription.get("result"), list) or subscription.get("error") is not None:
            raise ProtocolError("pool subscription rejected")
        password = f"d={difficulty}" if difficulty is not None else "x"
        if not accepted(await pool.request("mining.authorize", [worker, password])):
            raise ProtocolError("template check authorization rejected")
        job = await asyncio.wait_for(first_job, timeout)
        if job is None:
            raise ProtocolError("pool disconnected before sending a template")
        if not isinstance(job, list) or len(job) != 9 or not isinstance(job[2], str):
            raise ProtocolError("invalid template notification")
        coinbase = bytes.fromhex(job[2]).lower()
        return job[0], b"/mined by " in coinbase
    finally:
        await pool.close()


async def probe(reader, writer, pool_endpoint, client, fee, timeout, *, miner_worker=None, difficulty=None):
    """Record a pair of distinct pool-accepted submissions on the same job."""
    pending_notices = []
    authorized = False
    job = None
    epoch = 0
    first_proof = None
    miner_worker = miner_worker or client
    pool_password = f"d={difficulty}" if difficulty is not None else "x"

    async def notify(channel, packet):
        nonlocal job, epoch, first_proof
        method = packet["method"]
        if method == "client.reconnect":
            raise ProtocolError("pool requested a different session")
        if method == "mining.set_difficulty":
            print(f"pool difficulty={packet['params'][0]!s}", flush=True)
        if method in {"mining.notify", "mining.set_extranonce", "mining.set_version_mask", "mining.set_difficulty"}:
            if method == "mining.notify":
                job = packet["params"][0]
            epoch += 1
            first_proof = None
        if authorized:
            writer.write(encode_packet(packet))
            await asyncio.wait_for(writer.drain(), timeout)
        else:
            if len(pending_notices) >= 32:
                raise ProtocolError("too many notifications before authorization")
            pending_notices.append(packet)

    async def disconnected(channel):
        writer.close()

    pool_reader, pool_writer = await asyncio.wait_for(asyncio.open_connection(*pool_endpoint), timeout)
    pool = UpstreamConnection(pool_reader, pool_writer, "probe", notify, disconnected, timeout)
    miner = PacketReader(reader, max_bytes=4096, role="miner")
    try:
        while packet := await miner.read_packet():
            ident, method, params = packet.get("id"), packet.get("method"), packet.get("params", [])
            if type(ident) not in (int, str) or not isinstance(params, list):
                raise ProtocolError("invalid miner request")
            use_fee = False
            if method == "mining.authorize":
                if authorized or len(params) != 2 or params[0] != miner_worker:
                    raise ProtocolError("unexpected miner worker")
                reply = await pool.request(method, [client, pool_password])
                if not accepted(reply):
                    raise ProtocolError("client probe authorization rejected")
                reply = await pool.request(method, [fee, pool_password])
                if not accepted(reply):
                    raise ProtocolError("fee probe authorization rejected")
                authorized = True
            elif method == "mining.submit":
                if (not authorized or len(params) not in (5, 6) or params[0] != miner_worker
                        or not all(isinstance(value, str) for value in params[1:])):
                    raise ProtocolError("invalid probe submission")
                share_job = params[1]
                proof = tuple(value.lower() for value in params[2:])
                submission_epoch = epoch
                if share_job == job and first_proof == proof:
                    raise ProtocolError("duplicate proof from miner")
                use_fee = share_job == job and first_proof is not None
                reply = await pool.request(method, [fee if use_fee else client, *params[1:]])
                if use_fee:
                    print(f"fee_submit job_id={share_job!r} accepted={accepted(reply)} "
                          f"context_changed={submission_epoch != epoch} error={reply.get('error')!r}", flush=True)
                elif accepted(reply) and share_job == job and submission_epoch == epoch:
                    first_proof = proof
                    print(f"client_submit job_id={share_job!r} accepted=True", flush=True)
            elif method in {"mining.configure", "mining.subscribe", "mining.extranonce.subscribe"}:
                reply = await pool.request(method, params)
            else:
                reply = {"result": False, "error": [20, "unsupported probe request", None]}
            writer.write(encode_packet({**reply, "id": ident}))
            if authorized and pending_notices:
                for notice in pending_notices:
                    writer.write(encode_packet(notice))
                pending_notices.clear()
            await asyncio.wait_for(writer.drain(), timeout)
            if use_fee:
                return accepted(reply)
        return False
    finally:
        await pool.close()


async def run(args):
    try:
        templates = await asyncio.gather(*(check_template(VIABTC, worker, args.difficulty)
                                           for worker in (args.client, args.fee)))
    except (OSError, ValueError, ProtocolError, asyncio.TimeoutError) as error:
        print(f"template check failed: {error}", flush=True)
        return False
    for label, (job_id, account_bound) in zip(("client", "fee"), templates):
        print(f"{label} template job_id={job_id!r} account_tag={account_bound}", flush=True)
    if any(account_bound for _, account_bound in templates):
        print("probe stopped before opening a miner listener: account tag found in coinbase", flush=True)
        return False
    if args.check_templates:
        print("no account tag observed; same-job submission compatibility is unverified", flush=True)
        return True

    result = asyncio.get_running_loop().create_future()
    started = False

    async def handle(reader, writer):
        nonlocal started
        try:
            if started or writer.get_extra_info("peername")[0] != str(args.miner_ip):
                return
            started = True
            print("probe connected", flush=True)
            success = await asyncio.wait_for(
                probe(reader, writer, VIABTC, args.client, args.fee, 5,
                      miner_worker=args.miner_worker, difficulty=args.difficulty), args.seconds
            )
            result.set_result(success)
        except (OSError, ValueError, ProtocolError, asyncio.TimeoutError) as error:
            print(f"probe stopped: {type(error).__name__}", flush=True)
            if not result.done():
                result.set_result(False)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async with await asyncio.start_server(handle, str(args.bind), args.port, backlog=4) as server:
        print(f"Waiting for one ASIC on {server.sockets[0].getsockname()}", flush=True)
        try:
            return await asyncio.wait_for(result, args.seconds * 2)
        except asyncio.TimeoutError:
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--fee", required=True)
    parser.add_argument("--check-templates", action="store_true", help="inspect pool templates without an ASIC")
    parser.add_argument("--miner-ip", type=IPv4Address)
    parser.add_argument("--miner-worker", help="existing ASIC worker name; defaults to --client")
    parser.add_argument("--difficulty", type=int, help="request the same initial difficulty for both workers")
    parser.add_argument("--bind", type=IPv4Address, default="127.0.0.1")
    parser.add_argument("--port", default=13333, type=int)
    parser.add_argument("--seconds", default=90, type=int, choices=range(10, 181))
    args = parser.parse_args()
    if not args.check_templates and args.miner_ip is None:
        parser.error("--miner-ip is required for a miner probe")
    if args.difficulty is not None and not 1 <= args.difficulty <= 2 ** 31 - 1:
        parser.error("difficulty must be between 1 and 2147483647")
    accounts = [worker.partition(".")[0].casefold() for worker in (args.client, args.fee)]
    if len(set(accounts)) != 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-z0-9]{1,64}", worker) for worker in (args.client, args.fee)
    ):
        parser.error("provide dedicated worker names from two distinct accounts you own")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
