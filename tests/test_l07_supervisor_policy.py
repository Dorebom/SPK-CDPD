"""Synthetic Windows-policy facts only; zero native Windows API acceptance."""
from __future__ import annotations

from dataclasses import replace
import os

import pytest

from cdpd_chatgpt_profile.supervisor_policy import (
    AccessEntry, SecurityFacts, StoragePolicyError, WindowsStoragePolicy,
)


STATE = r"C:\Private\CDPDState"
PROFILE = r"C:\Private\ChromeProfile"
USER = "S-1-5-21-111-222-333-1001"
FULL = 0x001F01FF


class SyntheticFacts:
    user_sid = USER

    def __init__(self):
        self.attribute_overrides = {}
        self.security_overrides = {}
        self.volume_value = (3, "NTFS")
        self.cloud_outside = True

    def attributes(self, path):
        return self.attribute_overrides.get(path, 0x10)

    def volume(self, path):
        return self.volume_value

    def outside_cloud_root(self, path):
        if isinstance(self.cloud_outside, Exception):
            raise self.cloud_outside
        return self.cloud_outside

    def security(self, path):
        return self.security_overrides.get(path, SecurityFacts(
            USER, True, True,
            (AccessEntry(0, 3, FULL, USER), AccessEntry(0, 3, FULL, "S-1-5-18"),
             AccessEntry(0, 3, FULL, "S-1-5-32-544"))))


def policy(probe=None):
    return WindowsStoragePolicy(STATE, PROFILE, test_only=True,
                                native_probe=probe or SyntheticFacts())


def test_explicit_synthetic_positive_is_not_native_qualification():
    value = policy()
    value.validate_startup()
    assert value(STATE + r"\incidents\S0.json")
    assert value.test_only is True
    assert value.native_qualified is False
    assert value.last_failure == "NONE"


def test_windows_policy_does_not_silently_accept_linux():
    if os.name == "nt":
        pytest.skip("This negative is the non-Windows gate, not a native positive")
    with pytest.raises(StoragePolicyError, match="WINDOWS_REQUIRED"):
        WindowsStoragePolicy(STATE, PROFILE)


def test_injected_policy_requires_explicit_test_only():
    with pytest.raises(StoragePolicyError, match="INPUT_INVALID"):
        WindowsStoragePolicy(STATE, PROFILE, native_probe=SyntheticFacts())
    with pytest.raises(StoragePolicyError, match="INPUT_INVALID"):
        WindowsStoragePolicy(STATE, PROFILE, test_only=True)


@pytest.mark.parametrize("path", [
    r"\\server\share\state", r"\\?\C:\State", r"C:relative", "relative",
    r"C:\State\..\Other", r"C:\State\file:stream", "C:\\State\\space ",
    r"C:\State\dot.", r"C:\State\CON.txt", "C:\\State\\\nsecret",
])
def test_ambiguous_remote_device_or_alternate_stream_path_rejected(path):
    with pytest.raises(StoragePolicyError, match="INPUT_INVALID"):
        WindowsStoragePolicy(path, PROFILE, test_only=True, native_probe=SyntheticFacts())


@pytest.mark.parametrize("root,profile", [
    (STATE, STATE), (STATE, STATE + r"\profile"),
    (PROFILE + r"\state", PROFILE), ("C:\\", PROFILE),
])
def test_state_root_and_profile_are_distinct_scopes(root, profile):
    with pytest.raises(StoragePolicyError, match="INPUT_INVALID"):
        WindowsStoragePolicy(root, profile, test_only=True, native_probe=SyntheticFacts())


@pytest.mark.parametrize("path", [PROFILE, STATE + "_other\\file.json", r"D:\Private\CDPDState\file.json"])
def test_output_must_remain_under_exact_state_root(path):
    value = policy()
    assert not value(path)
    assert value.last_failure == "PATH_OUTSIDE_ROOT"


@pytest.mark.parametrize("attribute", [0x400, 0x1000, 0x40000, 0x80000, 0x100000, 0x400000])
def test_any_reparse_or_cloud_attribute_in_ancestor_is_rejected(attribute):
    probe = SyntheticFacts()
    probe.attribute_overrides[r"C:\Private"] = 0x10 | attribute
    with pytest.raises(StoragePolicyError, match="REPARSE_OR_CLOUD_PATH"):
        policy(probe).validate_startup()


@pytest.mark.parametrize("volume", [(4, "NTFS"), (2, "NTFS"), (3, "ReFS"), (3, "exFAT"), (0, "")])
def test_only_local_fixed_ntfs_is_admitted(volume):
    probe = SyntheticFacts()
    probe.volume_value = volume
    with pytest.raises(StoragePolicyError, match="LOCAL_NTFS_REQUIRED"):
        policy(probe).validate_startup()


def test_owner_and_profile_owner_must_match_current_token():
    probe = SyntheticFacts()
    probe.security_overrides[PROFILE] = replace(probe.security(PROFILE), owner_sid="S-1-5-21-other")
    with pytest.raises(StoragePolicyError, match="OWNER_MISMATCH"):
        policy(probe).validate_startup()


@pytest.mark.parametrize("mutation", [
    {"dacl_present": False}, {"dacl_protected": False}, {"entries": ()},
    {"entries": (AccessEntry(0, 0, FULL, "S-1-1-0"),)},
    {"entries": (AccessEntry(0, 0, FULL, "S-1-5-11"),)},
    {"entries": (AccessEntry(0, 0, FULL, "S-1-5-32-545"),)},
    {"entries": (AccessEntry(0, 8, FULL, USER),)},
    {"entries": (AccessEntry(0, 0, 1, USER),)},
    {"entries": (AccessEntry(5, 0, FULL, USER),)},
    {"entries": (AccessEntry(1, 0, FULL, USER),)},
])
def test_null_broad_unprotected_or_unrecognized_acl_is_rejected(mutation):
    probe = SyntheticFacts()
    probe.security_overrides[STATE] = replace(probe.security(STATE), **mutation)
    with pytest.raises(StoragePolicyError, match="PRIVATE_ACL_REQUIRED"):
        policy(probe).validate_startup()


def test_child_acl_is_rechecked_and_old_positive_is_not_cached():
    probe = SyntheticFacts()
    value = policy(probe)
    child = STATE + r"\incidents"
    assert value(child)
    probe.security_overrides[child] = replace(probe.security(child),
                                            entries=(AccessEntry(0, 0, FULL, "S-1-1-0"),))
    assert not value(child + r"\S0.json")
    assert value.last_failure == "PRIVATE_ACL_REQUIRED"


def test_missing_child_is_allowed_only_after_existing_private_ancestor():
    probe = SyntheticFacts()
    probe.attribute_overrides[STATE + r"\new"] = None
    assert policy(probe)(STATE + r"\new\S0.json")
    probe.attribute_overrides[STATE] = None
    with pytest.raises(StoragePolicyError, match="PATH_MISSING"):
        policy(probe).validate_startup()


@pytest.mark.parametrize("result", [False, None, "true", StoragePolicyError("CLOUD_SCOPE_UNKNOWN")])
def test_cloud_api_denial_or_unknown_never_proves_safe_storage(result):
    probe = SyntheticFacts()
    probe.cloud_outside = result
    assert not policy(probe)(STATE + r"\file.json")


def test_known_sync_namespace_rejected_even_if_native_probe_says_outside(monkeypatch):
    monkeypatch.setenv("OneDrive", r"C:\Private")
    with pytest.raises(StoragePolicyError, match="REPARSE_OR_CLOUD_PATH"):
        policy().validate_startup()


def test_policy_does_not_repair_any_input():
    probe = SyntheticFacts()
    probe.security_overrides[STATE] = replace(probe.security(STATE), dacl_protected=False)
    before = dict(probe.security_overrides)
    value = policy(probe)
    assert not value(STATE)
    assert probe.security_overrides == before
