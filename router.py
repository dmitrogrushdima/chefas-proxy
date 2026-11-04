"""Transparent stream forwarding with passive observation and backpressure."""


async def copy_stream(source, destination, direction, observer, metrics, on_activity):
    while data := await source.read(65536):
        on_activity()
        destination.write(data)
        try:
            observer.feed(direction, data)
        except Exception:
            metrics.observation_errors += 1
        await destination.drain()
