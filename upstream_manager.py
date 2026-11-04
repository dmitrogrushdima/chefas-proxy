"""Choose an upstream for a new session; never migrate an active session."""
import asyncio
import ssl


async def connect_upstream(primary, backup, tls_context, timeout, is_self_route, metrics):
    endpoints = [primary] + ([backup] if backup else [])
    for index, endpoint in enumerate(endpoints):
        try:
            if await is_self_route(endpoint):
                metrics.blocked_loops += 1
                metrics.upstream_failures += 1
                continue
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(*endpoint, ssl=tls_context), timeout
            )
            if index:
                metrics.backup_connections += 1
            return reader, writer, index
        except (OSError, asyncio.TimeoutError, ssl.SSLError):
            metrics.upstream_failures += 1
    return None, None, None
