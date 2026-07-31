from __future__ import annotations

import time
from pathlib import Path

from simserver import auth, db, queue as q


def make_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    return conn


def test_hash_password_round_trips() -> None:
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("wrong password", hashed) is False


def test_verify_password_rejects_garbage_stored_value() -> None:
    assert auth.verify_password("anything", "not-a-valid-hash") is False


def test_hash_password_is_salted_differently_each_time() -> None:
    first = auth.hash_password("same password")
    second = auth.hash_password("same password")
    assert first != second
    assert auth.verify_password("same password", first)
    assert auth.verify_password("same password", second)


def test_create_session_and_get_session_user(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    user_id = q.create_user(conn, "alice", auth.hash_password("pw"))

    token = auth.create_session(conn, user_id)
    user = auth.get_session_user(conn, token)

    assert user is not None
    assert user["username"] == "alice"


def test_get_session_user_returns_none_for_unknown_token(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert auth.get_session_user(conn, "does-not-exist") is None


def test_get_session_user_returns_none_for_expired_session(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    user_id = q.create_user(conn, "alice", auth.hash_password("pw"))
    token = auth.create_session(conn, user_id, ttl_days=0)
    # ttl_days=0 -> expires_at == now (or in the past by the time we check)
    conn.execute(
        "UPDATE sessions SET expires_at = ? WHERE token = ?",
        (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600)), token),
    )

    assert auth.get_session_user(conn, token) is None


def test_delete_session_invalidates_it(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    user_id = q.create_user(conn, "alice", auth.hash_password("pw"))
    token = auth.create_session(conn, user_id)

    auth.delete_session(conn, token)

    assert auth.get_session_user(conn, token) is None
