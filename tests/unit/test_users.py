import pytest

from securecam.auth.store import AuthError, Permission, UserStore, has_permission, validate_password

GOOD_PASSWORD = "correct-horse-battery"


def make_store(tmp_path):
    return UserStore(str(tmp_path / "users.json"), "cam-1")


def test_add_and_authenticate(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "admin")
    user = store.authenticate("alice", GOOD_PASSWORD, "cam-1")
    assert user is not None
    assert user.role_for("cam-1") == "admin"


def test_authentication_is_case_insensitive_on_username(tmp_path):
    store = make_store(tmp_path)
    store.add_user("Alice", GOOD_PASSWORD, "viewer")
    assert store.authenticate("alice", GOOD_PASSWORD, "cam-1") is not None


def test_wrong_password_fails(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "viewer")
    assert store.authenticate("alice", "wrong-password-here", "cam-1") is None


def test_unknown_user_fails(tmp_path):
    assert make_store(tmp_path).authenticate("nobody", GOOD_PASSWORD, "cam-1") is None


def test_user_bound_to_another_device_is_rejected(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "viewer")
    assert store.authenticate("alice", GOOD_PASSWORD, "other-cam") is None


def test_disabled_user_cannot_log_in(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "admin")
    store.add_user("bob", GOOD_PASSWORD, "viewer")
    store.set_enabled("bob", False)
    assert store.authenticate("bob", GOOD_PASSWORD, "cam-1") is None


def test_password_change_invalidates_the_old_password(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "viewer")
    store.set_password("alice", "another-long-password")
    assert store.authenticate("alice", GOOD_PASSWORD, "cam-1") is None
    assert store.authenticate("alice", "another-long-password", "cam-1") is not None


def test_last_admin_is_protected(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "admin")
    with pytest.raises(AuthError):
        store.delete_user("alice")
    with pytest.raises(AuthError):
        store.set_role("alice", "viewer")
    with pytest.raises(AuthError):
        store.set_enabled("alice", False)


def test_second_admin_removes_the_protection(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "admin")
    store.add_user("bob", GOOD_PASSWORD, "admin")
    store.delete_user("alice")
    assert store.admin_count() == 1


def test_duplicate_usernames_are_refused(tmp_path):
    store = make_store(tmp_path)
    store.add_user("alice", GOOD_PASSWORD, "viewer")
    with pytest.raises(AuthError):
        store.add_user("alice", GOOD_PASSWORD, "viewer")


def test_invalid_username_is_refused(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(AuthError):
        store.add_user("alice smith", GOOD_PASSWORD, "viewer")


def test_short_password_is_refused(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(AuthError):
        store.add_user("alice", "short", "viewer")


def test_password_policy_message_is_actionable():
    with pytest.raises(AuthError) as excinfo:
        validate_password("abc")
    assert "characters" in str(excinfo.value)


def test_store_survives_a_restart(tmp_path):
    make_store(tmp_path).add_user("alice", GOOD_PASSWORD, "admin")
    reopened = make_store(tmp_path)
    assert reopened.authenticate("alice", GOOD_PASSWORD, "cam-1") is not None


def test_roles_grant_the_expected_permissions():
    assert has_permission("viewer", Permission.VIEW_LIVE)
    assert has_permission("viewer", Permission.VIEW_EVENTS)
    assert not has_permission("viewer", Permission.MANAGE_USERS)
    assert not has_permission("viewer", Permission.CONFIGURE)
    assert has_permission("admin", Permission.MANAGE_USERS)
    assert has_permission("admin", Permission.CONFIGURE)
    assert not has_permission("nonexistent-role", Permission.VIEW_LIVE)


def test_password_hash_is_never_stored_in_plain_text(tmp_path):
    path = tmp_path / "users.json"
    UserStore(str(path), "cam-1").add_user("alice", GOOD_PASSWORD, "admin")
    assert GOOD_PASSWORD not in path.read_text(encoding="utf-8")
