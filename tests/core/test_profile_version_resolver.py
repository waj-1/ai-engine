from core.profile_version_resolver import (
    set_profile_version, clear_profile_version, get_profile_version)


def test_default_is_none():
    clear_profile_version()
    assert get_profile_version() is None


def test_set_and_clear():
    set_profile_version("ver-123")
    assert get_profile_version() == "ver-123"
    clear_profile_version()
    assert get_profile_version() is None
