"""Execution QA: immediate local denial and bounded scoped shared notification."""
from dataclasses import replace
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'code'))
from chatgpt_web_api.target_guard import BindingGuard, GuardError
from cdpd_chatgpt_profile.supervisor_authority import NonblockingRestriction
from test_binding_guard import fixture_guard, bridge_fixture, record


class RestrictionControls(unittest.TestCase):
    def test_mode_latch_is_immediate_and_peer_checks_shared_confirmation(self):
        guard, authority, clock = fixture_guard()
        peer = BindingGuard(guard.record, authority, clock=clock)
        restriction = NonblockingRestriction(guard)
        restriction.impose()
        self.assertEqual(guard.mode, 'DIAGNOSTIC_ONLY')
        self.assertEqual(restriction.wait(), 'CONFIRMED')
        with self.assertRaises(GuardError): guard.check(mutation=True)
        with self.assertRaises(GuardError): peer.check(mutation=True)
        self.assertEqual(authority.calls,[('mode','DIAGNOSTIC_ONLY',guard.record.binding_id,1)])

    def test_blocked_callback_does_not_block_local_latch_and_late_success_never_upgrades(self):
        guard, authority, _ = fixture_guard(); release = threading.Event(); entered = threading.Event()
        original = authority.set_mode
        def blocked(*args, **kwargs):
            entered.set(); release.wait(1); original(*args, **kwargs)
        authority.set_mode = blocked
        restriction = NonblockingRestriction(guard, timeout_seconds=.03)
        started = time.monotonic(); restriction.impose()
        try:
            self.assertLess(time.monotonic()-started,.1)
            self.assertEqual(guard.mode,'DIAGNOSTIC_ONLY')
            self.assertTrue(entered.wait(.1))
            self.assertEqual(restriction.wait(.1),'UNCONFIRMED')
            self.assertEqual(restriction.reason_code,'NOTIFICATION_TIMEOUT')
        finally: release.set()
        time.sleep(.03)
        self.assertEqual(restriction.status,'UNCONFIRMED')
        restriction.impose(); self.assertEqual(restriction.notification_attempts,1)

    def test_nonapplying_authority_cannot_be_confirmed(self):
        guard, authority, _ = fixture_guard()
        authority.set_mode = lambda *args, **kwargs: None
        restriction = NonblockingRestriction(guard)
        restriction.impose(); self.assertEqual(restriction.wait(),'UNCONFIRMED')
        self.assertFalse(restriction.confirmed)
        self.assertEqual(guard.mode,'DIAGNOSTIC_ONLY')

    def test_blocked_readback_seals_unknown(self):
        guard, authority, _ = fixture_guard(); release=threading.Event()
        original=authority.snapshot
        def blocked(): release.wait(1); return original()
        authority.snapshot=blocked
        restriction=NonblockingRestriction(guard,timeout_seconds=.02)
        restriction.impose()
        try: self.assertEqual(restriction.wait(.1),'UNCONFIRMED')
        finally: release.set()
        time.sleep(.02); self.assertFalse(restriction.confirmed)

    def test_reconciliation_mode_is_never_lowered(self):
        guard, authority, _ = fixture_guard(); guard.require_reconciliation(); authority.calls.clear()
        restriction=NonblockingRestriction(guard)
        restriction.impose(); self.assertEqual(restriction.wait(),'CONFIRMED')
        self.assertEqual(guard.mode,'RECONCILIATION_REQUIRED')
        self.assertEqual(authority.current.mode,'RECONCILIATION_REQUIRED')
        self.assertEqual(authority.calls[0][1],'RECONCILIATION_REQUIRED')

    def test_revoke_latch_is_terminal_before_callback_returns(self):
        guard, authority, _ = fixture_guard(); release=threading.Event()
        original=authority.invalidate
        def blocked(*args,**kwargs): release.wait(1); original(*args,**kwargs)
        authority.invalidate=blocked
        restriction=NonblockingRestriction(guard,timeout_seconds=.02)
        restriction.impose(revoke=True)
        try:
            self.assertEqual(guard.state,'INVALIDATED'); self.assertEqual(guard.reason,'BINDING_REVOKED')
            with self.assertRaises(GuardError): guard.check()
            self.assertEqual(restriction.wait(.1),'UNCONFIRMED')
        finally: release.set()

    def test_already_terminal_first_cause_and_state_are_preserved(self):
        guard, authority, _ = fixture_guard(); guard.invalidate('BINDING_EXPIRED'); authority.calls.clear()
        restriction=NonblockingRestriction(guard)
        restriction.impose(revoke=True)
        self.assertEqual(restriction.wait(),'CONFIRMED')
        self.assertEqual((guard.state,guard.reason),('EXPIRED','BINDING_EXPIRED'))
        self.assertEqual(authority.calls,[('invalidate','BINDING_EXPIRED',guard.record.binding_id,1)])

    def test_idempotence_and_confirmed_mode_to_revoke_upgrade(self):
        guard, authority, _ = fixture_guard(); restriction=NonblockingRestriction(guard)
        restriction.impose(); self.assertEqual(restriction.wait(),'CONFIRMED')
        for _ in range(5): restriction.impose()
        restriction.impose(revoke=True); self.assertEqual(restriction.wait(),'CONFIRMED')
        for _ in range(5): restriction.impose(revoke=True); restriction.impose()
        self.assertEqual(restriction.notification_attempts,2)
        self.assertEqual(len(authority.calls),2)
        self.assertEqual(restriction.target,'REVOKE')
        self.assertTrue(guard._terminal)

    def test_upgrade_during_pending_notice_is_local_only_and_sticky_unknown(self):
        guard, authority, _ = fixture_guard(); release=threading.Event(); entered=threading.Event()
        original=authority.set_mode
        def blocked(*args,**kwargs): entered.set(); release.wait(1); original(*args,**kwargs)
        authority.set_mode=blocked
        restriction=NonblockingRestriction(guard)
        restriction.impose(); self.assertTrue(entered.wait(.1))
        try:
            restriction.impose(revoke=True)
            self.assertEqual(restriction.status,'UNCONFIRMED')
            self.assertEqual(guard.reason,'BINDING_REVOKED')
            self.assertEqual(restriction.notification_attempts,1)
        finally: release.set()
        time.sleep(.02)
        self.assertEqual(restriction.status,'UNCONFIRMED')
        self.assertFalse(any(c[0]=='invalidate' for c in authority.calls))

    def test_unknown_mode_to_revoke_does_not_retry_authority(self):
        guard, authority, _ = fixture_guard(); calls=[]
        authority.set_mode=lambda *args,**kwargs: calls.append((args,kwargs))
        restriction=NonblockingRestriction(guard)
        restriction.impose(); self.assertEqual(restriction.wait(),'UNCONFIRMED')
        restriction.impose(revoke=True)
        self.assertEqual(restriction.status,'UNCONFIRMED')
        self.assertTrue(guard._terminal)
        self.assertEqual(len(calls),1); self.assertEqual(restriction.notification_attempts,1)

    def test_late_callback_is_scoped_to_old_generation(self):
        guard, authority, _ = fixture_guard(); release=threading.Event(); entered=threading.Event()
        original=authority.set_mode
        def blocked(*args,**kwargs): entered.set(); release.wait(1); original(*args,**kwargs)
        authority.set_mode=blocked
        restriction=NonblockingRestriction(guard,timeout_seconds=.02)
        restriction.impose(); self.assertTrue(entered.wait(.1))
        try:
            authority.current=replace(authority.current,generation=2,mode='NORMAL')
            self.assertEqual(restriction.wait(.1),'UNCONFIRMED')
        finally: release.set()
        time.sleep(.02)
        self.assertEqual(authority.current.generation,2); self.assertEqual(authority.current.mode,'NORMAL')
        self.assertEqual(authority.calls[-1],('mode','DIAGNOSTIC_ONLY',guard.record.binding_id,1))
        self.assertFalse(restriction.confirmed)

    def test_changed_local_record_is_not_restricted_as_old_scope(self):
        guard, authority, _ = fixture_guard(); restriction=NonblockingRestriction(guard)
        guard.record=record(binding_id='33333333-3333-4333-8333-333333333333')
        restriction.impose(revoke=True)
        self.assertEqual(restriction.status,'UNCONFIRMED')
        self.assertEqual(guard.mode,'NORMAL'); self.assertFalse(guard._terminal)
        self.assertEqual(authority.calls,[])

    def test_wrong_record_hash_readback_is_unconfirmed(self):
        guard, authority, _ = fixture_guard()
        original=authority.set_mode
        def bad(*args,**kwargs): original(*args,**kwargs); authority.current=replace(authority.current,record_hash='0'*64)
        authority.set_mode=bad
        restriction=NonblockingRestriction(guard)
        restriction.impose(); self.assertEqual(restriction.wait(),'UNCONFIRMED')

    def test_readback_exception_is_not_revocation_proof_for_duck_authority(self):
        guard, authority, _ = fixture_guard()
        authority.snapshot=lambda: (_ for _ in ()).throw(RuntimeError('PRIVATE'))
        restriction=NonblockingRestriction(guard)
        restriction.impose(revoke=True)
        self.assertEqual(restriction.wait(),'UNCONFIRMED')
        self.assertNotIn('PRIVATE',restriction.reason_code)

    def test_accepted_bridge_terminal_snapshot_failure_uses_exact_locked_state(self):
        bridge, owner, clock, _ = bridge_fixture()
        guard=bridge.create_guard(); restriction=NonblockingRestriction(guard)
        restriction.impose(revoke=True)
        self.assertEqual(restriction.wait(),'CONFIRMED')
        self.assertEqual(guard.state,'INVALIDATED')

    def test_invalid_timeouts_and_nonboolean_revoke_are_rejected(self):
        guard, _, _=fixture_guard()
        for value in [True,0,-1,float('nan'),.251]:
            with self.assertRaises(ValueError): NonblockingRestriction(guard,value)
        restriction=NonblockingRestriction(guard)
        with self.assertRaises(ValueError): restriction.impose(revoke=1)
        with self.assertRaises(ValueError): restriction.wait(.251)
        self.assertEqual(restriction.wait(0),'NOT_IMPOSED')


if __name__=='__main__': unittest.main()
