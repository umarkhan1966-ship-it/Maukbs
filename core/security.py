"""Password hashing + session authentication."""
import hashlib
import secrets
from fastapi.responses import RedirectResponse
from core.db import q

PBKDF2_ITERATIONS = 200_000


def hash_password(pw: str) -> str:
    """Return a salted PBKDF2 hash string: pbkdf2_sha256$iters$salt$hash."""
    salt = secrets.token_bytes(16)
    dk   = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    """Verify a password against a stored hash.

    Supports the new salted PBKDF2 format and falls back to the legacy
    unsalted SHA-256 hashes so existing accounts keep working until they
    log in once (login upgrades them automatically)."""
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode(),
                                     bytes.fromhex(salt_hex), int(iters))
            return secrets.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    # Legacy unsalted SHA-256
    legacy = hashlib.sha256(pw.encode()).hexdigest()
    return secrets.compare_digest(legacy, stored)


def get_session(token: str | None) -> dict | None:
    """Return the logged-in user dict for a valid, unexpired session token."""
    if not token:
        return None
    rows = q("""SELECT u.* FROM sessions s
                JOIN users u ON u.username = s.username
                WHERE s.token = ?
                  AND s.expires_at > datetime('now')
                  AND u.is_active = 1""",
             (token,), fetch=True)
    return dict(rows[0]) if rows else None


def require_login(token: str | None):
    """Redirect to login if not authenticated."""
    user = get_session(token)
    if not user:
        return RedirectResponse("/login", status_code=303), None
    return None, user
