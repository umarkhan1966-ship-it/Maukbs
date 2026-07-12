"""auth routes."""
import os, io, re, uuid, math, shutil, secrets, hashlib, html
from datetime import datetime, timedelta, date
from fastapi import APIRouter, Request, Form, Cookie, UploadFile, File
from fastapi.responses import (HTMLResponse, RedirectResponse, FileResponse,
                               JSONResponse, StreamingResponse, Response,
                               PlainTextResponse)
from core.db import DB_FILE, db, q
from core.constants import *
from core.security import (hash_password, verify_password,
                           get_session, require_login)
from core.layout import page
from core.rota_utils import (calc_paid_hours, parse_hours,
                             get_week_start, get_week_dates)

router = APIRouter()

# ── Brute-force guard: throttle failed logins per client IP ──
# In-memory (single instance); resets on restart, which is fine for our scale.
import time as _time
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOCK_MAX    = 8      # this many failed attempts…
_LOCK_WINDOW = 900    # …within this many seconds → temporary block


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:                                   # behind a host's proxy → real IP
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _too_many(ip: str) -> bool:
    now  = _time.time()
    hits = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOCK_WINDOW]
    _LOGIN_FAILS[ip] = hits
    return len(hits) >= _LOCK_MAX


def _record_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(_time.time())


# Send the Secure flag on the session cookie in production (HTTPS). Set the env
# var SECURE_COOKIES=1 on the host; leave unset for local http on 127.0.0.1.
_SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "") == "1"


@router.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    # Escape the error text before reflecting it into the page, so a crafted
    # /login?error=... link can't inject HTML/script (reflected-XSS guard).
    err_html = f"<p class='flash-error'>{html.escape(error)}</p>" if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>BusinessVault — Sign In</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600;700;900&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
  <style>body{{font-family:'DM Sans',sans-serif;}}</style>
</head>
<body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
  <div class="w-full max-w-sm">
    <div class="text-center mb-8">
      <div class="text-3xl font-black text-slate-800 tracking-tight">BusinessVault</div>
      <div class="text-slate-500 text-sm mt-1">MAUKBs Ltd · Management System</div>
    </div>
    <div style="background:white;border-radius:20px;padding:32px;border:1px solid #e2e8f0;box-shadow:0 4px 24px rgba(0,0,0,.06)">
      {err_html}
      <form action="/login" method="POST" autocomplete="off" class="space-y-4 {'mt-4' if error else ''}">
        <div>
          <label style="font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px">Username</label>
          <input name="username" type="text" required autofocus autocomplete="off"
            style="width:100%;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:15px;outline:none;font-family:'DM Sans',sans-serif;">
        </div>
        <div>
          <label style="font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px">Password</label>
          <input name="password" type="password" required autocomplete="off"
            style="width:100%;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:15px;outline:none;font-family:'DM Sans',sans-serif;">
        </div>
        <button type="submit"
          style="width:100%;background:#0f2942;color:white;font-weight:700;padding:12px;border-radius:10px;font-size:15px;border:none;cursor:pointer;font-family:'DM Sans',sans-serif;margin-top:4px;">
          Sign In →
        </button>
      </form>
    </div>
    <p class="text-center text-xs text-slate-400 mt-6">MAUKBs Ltd · Authorised users only</p>
  </div>
</body>
</html>"""


@router.post("/login")
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    if _too_many(ip):
        return RedirectResponse(
            "/login?error=Too+many+attempts.+Please+wait+a+few+minutes+and+try+again.",
            status_code=303)

    rows = q("SELECT * FROM users WHERE username=? AND is_active=1",
             (username,), fetch=True)
    user = dict(rows[0]) if rows else None
    if not user or not verify_password(password, user["password"]):
        _record_fail(ip)
        return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)

    _LOGIN_FAILS.pop(ip, None)   # successful login clears the counter

    # Transparently upgrade legacy unsalted hashes on successful login.
    if not user["password"].startswith("pbkdf2_sha256$"):
        q("UPDATE users SET password=? WHERE username=?",
          (hash_password(password), username))

    # Clear out expired sessions, then issue a fresh random token.
    q("DELETE FROM sessions WHERE expires_at <= datetime('now')")
    token = secrets.token_urlsafe(32)
    q("INSERT INTO sessions (token, username, expires_at) "
      "VALUES (?, ?, datetime('now', '+7 days'))",
      (token, username))

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True, samesite="lax",
                    secure=_SECURE_COOKIES, max_age=86400 * 7)
    return resp


@router.get("/logout")
def do_logout(session: str | None = Cookie(default=None)):
    if session:
        q("DELETE FROM sessions WHERE token=?", (session,))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp
