import asyncio
from fractions import Fraction

import pytest

from devfee_scheduler import AcceptedWorkLedger, FairnessAllocator
from history_store import DevFeeJournal


def test_loss_reduces_real_future_fee_budget_without_creating_accepted_work():
    ledger = AcceptedWorkLedger(loss_protection=True)
    ledger.record('user', 99)
    ledger.record('fee', 1)
    assert ledger.protect_loss(1)
    ledger.record('user', 99)
    assert not FairnessAllocator(ledger).acquire('miner', 1)
    assert ledger.user_work == 198 and ledger.fee_work == 1
    ledger.record('user', 99)
    assert FairnessAllocator(ledger).acquire('miner', 1)
    ledger.record('fee', 1)
    assert ledger.user_work == Fraction(99, 100) * (ledger.user_work + ledger.fee_work + 1)
    snapshot = ledger.snapshot()
    assert snapshot['protected_loss_difficulty'] == '1'
    assert snapshot['user_accepted_shares'] == 3
    assert snapshot['fee_percentage'] < 1


def test_loss_cannot_create_negative_fee_income_or_change_manual_policy():
    ledger = AcceptedWorkLedger(loss_protection=True)
    ledger.protect_loss(10)
    assert not ledger.should_fee()
    assert ledger.snapshot()['net_fee_target_difficulty'] == '0'
    assert ledger.snapshot()['unfunded_loss_difficulty'] == '10'
    assert ledger.snapshot()['fee_excess_difficulty'] == '0'
    with pytest.raises(ValueError):
        ledger.set_fee_percent(50)
    old = ledger.protected_loss_work
    for invalid in (0, -1, True, 'NaN'):
        with pytest.raises(ValueError):
            ledger.protect_loss(invalid)
    assert ledger.protected_loss_work == old
    disabled = AcceptedWorkLedger()
    assert not disabled.protect_loss(10)
    disabled.record('user', 99)
    assert disabled.should_fee()


def test_loss_journal_is_atomic_persistent_and_tenant_isolated(tmp_path):
    async def scenario():
        path = tmp_path / 'fee.sqlite3'
        journal = DevFeeJournal(path)
        assert await journal.record('user', 'pool', 'accepted', 198, True, 'a')
        assert await journal.record('fee', 'pool', 'accepted', 1, False, 'a')
        assert await journal.record('user', 'pool', 'rejected', 1, False, 'a', compensated_loss=True)
        assert await journal.record('user', 'pool', 'accepted', 99, True, 'b')
        await journal.close()
        journal = DevFeeJournal(path)
        for tenant, expected in [('a', 0), ('b', 1), (None, 1)]:
            ledger = AcceptedWorkLedger(loss_protection=True)
            journal.restore_budget(ledger, tenant)
            assert ledger.outstanding == expected
            assert ledger.protected_loss_work == (0 if tenant == 'b' else 1)
        disabled = AcceptedWorkLedger()
        journal.restore_budget(disabled, 'a')
        assert disabled.outstanding == 0  # Disabling new protection cannot claw back old credits.
        journal.bind_loss_scope('account-a')
        journal.bind_loss_scope('account-a')
        with pytest.raises(ValueError):
            journal.bind_loss_scope('account-b')
        with pytest.raises(ValueError):
            journal.bind_loss_scope(None)
        # A loss row must roll back with an invalid outcome, not partially commit.
        assert not await journal.record('fee', 'pool', 'accepted', 1, False, 'a', compensated_loss=True)
        restored = AcceptedWorkLedger(loss_protection=True)
        journal.restore_budget(restored, 'a')
        assert restored.fee_work == 1 and restored.protected_loss_work == 1
        await journal.close()
    asyncio.run(scenario())
