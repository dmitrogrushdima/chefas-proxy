"""Loopback-only relay load experiment; synthetic bytes, no real pool or mining."""
import argparse
import asyncio
import json
import math
import time

from proxy import Relay


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * fraction) - 1)], 2)


async def run(clients, ramp_ms, timeout):
    if (not isinstance(clients, int) or not 1 <= clients <= 1000 or
            not math.isfinite(ramp_ms) or not 0 <= ramp_ms <= 1000 or
            not math.isfinite(timeout) or not 0.1 <= timeout <= 120):
        raise ValueError("invalid experiment parameters")
    async def echo(reader, writer):
        try:
            while data := await reader.readline():
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    pool = await asyncio.start_server(echo, "127.0.0.1", 0, backlog=1024)
    relay = Relay("127.0.0.1", pool.sockets[0].getsockname()[1], limit=clients, timeout=timeout)
    proxy = await asyncio.start_server(relay.handle, "127.0.0.1", 0, backlog=1024)
    port = proxy.sockets[0].getsockname()[1]
    relay.set_listeners(proxy)
    attempts_done = asyncio.Event()
    release = asyncio.Event()
    results = []
    attempts = 0
    start = time.monotonic()

    async def client(index):
        nonlocal attempts
        writer = None
        try:
            # Delay each launch to model either a burst or a controlled ramp.
            await asyncio.sleep(index * ramp_ms / 1000)
            started = time.monotonic()
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
                latency = (time.monotonic() - started) * 1000
                payload = f"client-{index}\n".encode()
                writer.write(payload)
                await writer.drain()
                reply = await asyncio.wait_for(reader.readline(), timeout)
                results.append((reply == payload, latency))
            except (OSError, asyncio.TimeoutError):
                results.append((False, None))
            finally:
                attempts += 1
                if attempts == clients:
                    attempts_done.set()
            await release.wait()
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    tasks = [asyncio.create_task(client(i)) for i in range(clients)]
    try:
        await attempts_done.wait()
        good = [latency for ok, latency in results if ok and latency is not None]
        return {"requested": clients, "successful_echoes": len(good), "failed": clients - len(good),
                "active_at_checkpoint": relay.active, "connect_p50_ms": percentile(good, .50),
                "connect_p95_ms": percentile(good, .95), "connect_max_ms": round(max(good), 2) if good else None,
                "elapsed_seconds": round(time.monotonic() - start, 2),
                "ramp_ms_per_client": ramp_ms, "pool": "local synthetic echo", "production_result": False}
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        proxy.close()
        pool.close()
        await proxy.wait_closed()
        await pool.wait_closed()
        await relay.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--ramp-ms", type=float, default=5)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.clients <= 1000 or not 0 <= args.ramp_ms <= 1000 or not 0.1 <= args.timeout <= 120:
        parser.error("invalid experiment parameters")
    result = asyncio.run(run(args.clients, args.ramp_ms, args.timeout))
    print(json.dumps(result, indent=2))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
