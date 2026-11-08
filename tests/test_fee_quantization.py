import csv
import os
from pathlib import Path
from fractions import Fraction
from random import Random

from devfee_scheduler import AcceptedWorkLedger, FairnessAllocator, FeeWindows


def test_quantized_fleet_profile():
    workers = 40
    duration = 6000
    clock = [0]
    ledger = AcceptedWorkLedger()
    allocator = FairnessAllocator(ledger)
    windows = FeeWindows(allocator, ttl=12, cooldown=10, overdraft=.02, clock=lambda: clock[0])
    for worker in range(workers):
        windows.join(worker, 'group')
    rng = Random(2048)
    started = {}
    durations = []
    selected = []
    user_off = peak_diverted = 0
    series = []
    for now in range(duration):
        clock[0] = now
        for worker in range(workers):
            if windows.acquire(worker, 4):
                if worker not in started:
                    started[worker] = now
                    selected.append(worker)
            elif worker in started:
                durations.append(now - started.pop(worker))
                windows.end(worker)
        peak_diverted = max(peak_diverted, len(started))
        previous_fee = ledger.fee_work
        for worker in range(workers):
            opportunity = rng.randrange(4)
            if worker not in started:
                ledger.record('user', 1)
            elif opportunity == 0:
                token = allocator.begin_submit(worker, 4)
                ledger.record('fee', 4)
                allocator.finish_submit(token)
            user_off += 1
        series.append((now, workers, workers - len(started), int(ledger.fee_work - previous_fee)))
    ratio = ledger.snapshot()['fee_percentage']
    print(f'ASICs={workers} ticks={duration} OFF_user_work={user_off} '
          f'ON_user_work={ledger.user_work} ON_fee_work={ledger.fee_work} '
          f'peak_diverted={peak_diverted}/{workers} max_window_s={max(durations)} '
          f'fee_pct={ratio:.5f} distinct_fee_workers={len(set(selected))}')
    assert peak_diverted == 1
    assert len(set(selected)) == workers
    assert max(durations) <= 12
    assert 0.99 <= ratio <= 1.01
    destination = os.environ.get("CHEFAS_QUANTIZATION_CSV")
    if destination:
        with Path(destination).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("simulated_second", "off_user_work", "on_user_work", "on_fee_accepted_work"))
            writer.writerows(series)


def test_window_expiry_cooldown_and_round_robin():
    clock = [0]
    ledger = AcceptedWorkLedger()
    ledger.record('user', 990)
    allocator = FairnessAllocator(ledger)
    windows = FeeWindows(allocator, ttl=4, cooldown=10, overdraft=0, clock=lambda: clock[0])
    for worker in range(3):
        windows.join(worker, 'group')
    assert not windows.acquire(0, 11)
    assert windows.acquire(0, 10)
    assert not windows.acquire(1, 10)
    assert not windows.acquire(2, 10)
    pending = allocator.begin_submit(0, 10)
    clock[0] = 4
    assert windows.expired(0)
    assert not windows.acquire(0, 10)
    windows.end(0)
    assert not windows.acquire(1, 10)
    allocator.finish_submit(pending, 'rejected')
    assert not windows.acquire(2, 10)
    assert windows.acquire(1, 10)
    assert allocator.reserved == Fraction(10)


def test_large_group_admits_light_worker_and_skips_overweight_worker():
    ledger = AcceptedWorkLedger()
    ledger.record('user', 9900)
    windows = FeeWindows(FairnessAllocator(ledger), ttl=60, cooldown=5, overdraft=0)
    for worker in range(51):
        windows.join(worker, 'group')
        windows.observe(worker, 10 if worker == 0 else 1)
    assert not windows.acquire(0, 1)
    assert windows.acquire(1, 1)
    assert not windows.acquire(2, 1)


def test_small_groups_have_short_windows_and_wait_for_full_quota():
    for count in (1, 2, 3, 5):
        clock = [0]
        ledger = AcceptedWorkLedger()
        windows = FeeWindows(FairnessAllocator(ledger), ttl=60, cooldown=5, overdraft=.02,
                             clock=lambda: clock[0])
        for worker in range(count):
            windows.join(worker, 'group')
        ledger.record('user', 97)
        assert not windows.acquire(0, 1)
        ledger.record('user', 1)
        assert windows.acquire(0, 1)
        clock[0] = 14.99
        assert not windows.expired(0)
        clock[0] = 15
        assert windows.expired(0)
        assert not windows.acquire(0, 1)
        windows.end(0)
        assert not windows.acquire(0, 1)


def test_sqlite_restores_each_tenant_quota_independently(tmp_path):
    import asyncio
    from history_store import DevFeeJournal

    async def scenario():
        path = tmp_path / "work.sqlite3"
        journal = DevFeeJournal(path)
        try:
            assert await journal.record("user", "primary", "accepted", 99, True, "rack-a")
            assert await journal.record("user", "primary", "accepted", 198, True, "rack-b")
            assert await journal.record("fee", "shared-fee", "accepted", 1, False, "rack-a")
        finally:
            await journal.close()
        journal = DevFeeJournal(path)
        try:
            a, b = AcceptedWorkLedger(), AcceptedWorkLedger()
            journal.restore_budget(a, "rack-a")
            journal.restore_budget(b, "rack-b")
            assert FairnessAllocator(a).outstanding == 0
            assert FairnessAllocator(b).outstanding == 2
        finally:
            await journal.close()

    asyncio.run(scenario())
