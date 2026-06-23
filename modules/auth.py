"""auth routes."""
import os, io, re, uuid, math, shutil, secrets, hashlib
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


@router.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    err_html = f"<p class='flash-error'>{error}</p>" if error else ""
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
      <div class="text-slate-500 text-sm mt-1">Maukbs Ltd · Management System</div>
    </div>
    <div style="background:white;border-radius:20px;padding:32px;border:1px solid #e2e8f0;box-shadow:0 4px 24px rgba(0,0,0,.06)">
      {err_html}
      <form action="/login" method="POST" class="space-y-4 {'mt-4' if error else ''}">
        <div>
          <label style="font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px">Username</label>
          <input name="username" type="text" required autofocus
            style="width:100%;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:15px;outline:none;font-family:'DM Sans',sans-serif;">
        </div>
        <div>
          <label style="font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px">Password</label>
          <input name="password" type="password" required
            style="width:100%;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:15px;outline:none;font-family:'DM Sans',sans-serif;">
        </div>
        <button type="submit"
          style="width:100%;background:#0f2942;color:white;font-weight:700;padding:12px;border-radius:10px;font-size:15px;border:none;cursor:pointer;font-family:'DM Sans',sans-serif;margin-top:4px;">
          Sign In →
        </button>
      </form>
    </div>
    <p class="text-center text-xs text-slate-400 mt-6">Maukbs Ltd · Authorised users only</p>
  </div>
</body>
</html>"""


@router.post("/login")
def do_login(username: str = Form(...), password: str = Form(...)):
    rows = q("SELECT * FROM users WHERE username=? AND is_active=1",
             (username,), fetch=True)
    user = dict(rows[0]) if rows else None
    if not user or not verify_password(password, user["password"]):
        return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)

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
                    max_age=86400 * 7)
    return resp


@router.get("/logout")
def do_logout(session: str | None = Cookie(default=None)):
    if session:
        q("DELETE FROM sessions WHERE token=?", (session,))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp
