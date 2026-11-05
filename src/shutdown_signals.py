"""Cross-platform cooperative shutdown signals; CLI installs/restores explicitly."""
import asyncio
import logging
import signal
from pilot_logging import event


class ShutdownSignals:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        self.previous = {}
        self.loop_handlers = set()

    def requested(self, number):
        if not self.stop.is_set():
            event(logging.INFO, 'shutdown_requested', signal=signal.Signals(number).name)
            self.stop.set()

    def install(self):
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, 'SIGBREAK'):
            signals.append(signal.SIGBREAK)
        try:
            for number in signals:
                self.previous[number] = signal.getsignal(number)
                try:
                    self.loop.add_signal_handler(number, self.requested, number)
                    self.loop_handlers.add(number)
                except NotImplementedError:
                    signal.signal(number, lambda sig, frame: self.loop.call_soon_threadsafe(self.requested, sig))
        except BaseException:
            self.close()
            raise

    def close(self):
        for number, previous in self.previous.items():
            if number in self.loop_handlers:
                self.loop.remove_signal_handler(number)
            signal.signal(number, previous)
        self.previous.clear()
        self.loop_handlers.clear()
