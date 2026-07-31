"""Web-client auth: password hashing and browser sessions.

A separate layer from the JSON API's static X-API-Key scheme (api.py's
require_job_key/require_admin_key) — this one is for individual researcher
accounts logging in through a browser, not programmatic callers.

Password hashing uses stdlib hashlib.pbkdf2_hmac rather than adding a
dependency (bcrypt/passlib/argon2) — consistent with the rest of the
project's preference for stdlib where it's sufficient, and PBKDF2 with a
reasonable iteration count is an accepted choice for this threat model
(internal tool, not a public-facing service processing untrusted signups).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"{salt}${_PBKDF2_ITERATIONS}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, iterations, hex_digest = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(digest.hex(), hex_digest)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def create_session(conn: sqlite3.Connection, user_id: int, *, ttl_days: int = 7) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + ttl_days * 86400))
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, _now(), expires_at),
    )
    return token


def get_session_user(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT users.* FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ? AND sessions.expires_at > ?
        """,
        (token, _now()),
    ).fetchone()
    return row


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
