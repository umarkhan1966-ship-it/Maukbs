"""User administration — OWNER ONLY.

Add users, set their role + store, reset passwords, and enable/disable
accounts. Passwords are only ever set (hashed) here, never displayed —
the owner cannot see anyone's password, only reset it to a new one.
"""
from urllib.parse import quote as uq
from fastapi import APIRouter, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from core.db import q
from core.security import hash_password, require_login
from core.layout import page

router = APIRouter()

ROLES  = ["owner", "manager", "staff"]
STORES = ["", "Uxbridge", "Newbury"]


def _guard_owner(session):
    """Return (redirect, user). Only owners get through; everyone else is
    bounced to the dashboard."""
    redir, user = require_login(session)
    if redir:
        return redir, None
    if user.get("role") != "owner":
        return RedirectResponse("/", status_code=303), None
    return None, user


def _opts(values, current):
    cur = current or ""
    return "".join(
        f"<option value='{v}' {'selected' if v == cur else ''}>{v or '(none)'}</option>"
        for v in values)


@router.get("/manage-users", response_class=HTMLResponse)
def manage_users(session: str | None = Cookie(default=None),
                 msg: str = "", msg_type: str = "success"):
    redir, user = _guard_owner(session)
    if redir:
        return redir

    users = q("""SELECT user_id, username, full_name, role, store_name, is_active
                 FROM users ORDER BY is_active DESC, role, full_name""",
              fetch=True) or []

    flash = ""
    if msg:
        cls = "flash-success" if msg_type == "success" else "flash-error"
        flash = f"<div class='{cls}'>{msg}</div>"

    rows_html = ""
    for u in users:
        uid     = u["user_id"]
        is_self = (uid == user["user_id"])
        active  = u["is_active"] == 1
        badge   = ("<span class='badge-paid'>Active</span>" if active
                   else "<span class='badge-unpaid'>Disabled</span>")
        who = (f"<div style='font-weight:700'>{u['full_name'] or ''}"
               f"{' <span style=\"font-size:11px;color:#94a3b8\">(you)</span>' if is_self else ''}</div>"
               f"<div style='font-size:11px;color:#94a3b8'>{u['username']}</div>")

        if is_self:
            # Never let the owner edit/disable their own role or account here
            # (avoids locking yourself out). Password reset is still allowed.
            role_store = (f"<span style='font-size:13px'>{u['role']}"
                          f"{' · ' + u['store_name'] if u['store_name'] else ''}</span>")
            toggle = "<span style='font-size:11px;color:#94a3b8'>—</span>"
        else:
            role_store = f"""
              <form method='POST' action='/manage-users/save/{uid}'
                    style='display:flex;gap:6px;align-items:center;flex-wrap:wrap'>
                <select name='role' style='font-size:12px'>{_opts(ROLES, u['role'])}</select>
                <select name='store_name' style='font-size:12px'>{_opts(STORES, u['store_name'])}</select>
                <button type='submit' class='btn-secondary' style='font-size:12px'>Save</button>
              </form>"""
            toggle = f"""
              <form method='POST' action='/manage-users/toggle/{uid}' style='display:inline'>
                <button type='submit' class='btn-secondary' style='font-size:12px'>
                  {'Disable' if active else 'Enable'}</button>
              </form>"""

        reset = f"""
          <form method='POST' action='/manage-users/reset/{uid}'
                style='display:flex;gap:6px;align-items:center'>
            <input type='text' name='password' placeholder='new temp password' required
                   style='font-size:12px;width:150px'>
            <button type='submit' class='btn-secondary' style='font-size:12px'>Reset</button>
          </form>"""

        rows_html += (f"<tr><td>{who}</td><td>{role_store}</td>"
                      f"<td style='text-align:center'>{badge}</td>"
                      f"<td>{reset}</td><td style='text-align:center'>{toggle}</td></tr>")

    content = f"""
    {flash}
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>&#128273; Manage Users</div>
      <a href='/' class='btn-secondary'>&#8592; Dashboard</a>
    </div>

    <div class='card' style='margin-top:12px'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:8px'>&#10133; Add a user</div>
      <form method='POST' action='/manage-users/add' class='grid gap-3'
            style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr))'>
        <div><label>Full name</label><input name='full_name' required></div>
        <div><label>Username</label><input name='username' required placeholder='e.g. jane.smith'></div>
        <div><label>Role</label><select name='role'>{_opts(ROLES, 'staff')}</select></div>
        <div><label>Store</label><select name='store_name'>{_opts(STORES, '')}</select></div>
        <div><label>Temp password</label><input name='password' required></div>
        <div style='display:flex;align-items:flex-end'>
          <button type='submit' class='btn-primary'>Add user</button></div>
      </form>
      <div style='font-size:12px;color:#94a3b8;margin-top:8px'>
        Staff must be assigned to a store. Hand them the temporary password &mdash;
        they can change it themselves under My Profile.</div>
    </div>

    <div class='card' style='margin-top:12px'>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>User</th><th>Role / Store</th>
            <th style='text-align:center'>Status</th><th>Reset password</th>
            <th style='text-align:center'>Account</th></tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>
    """
    return page("Manage Users", content, user, "users")


@router.post("/manage-users/add")
def add_user(session: str | None = Cookie(default=None),
             full_name: str = Form(""), username: str = Form(""),
             role: str = Form("staff"), store_name: str = Form(""),
             password: str = Form("")):
    redir, user = _guard_owner(session)
    if redir:
        return redir
    username   = (username or "").strip().lower()
    full_name  = (full_name or "").strip()
    role       = role if role in ROLES else "staff"
    store_name = store_name if store_name in STORES else ""

    err = None
    if not username or not (password or "").strip():
        err = "Username and temporary password are required."
    elif role == "staff" and not store_name:
        err = "Staff users must be assigned to a store."
    elif q("SELECT 1 FROM users WHERE username=?", (username,), fetch=True):
        err = f"Username '{username}' already exists."
    if err:
        return RedirectResponse(f"/manage-users?msg={uq(err)}&msg_type=error", status_code=303)

    q("""INSERT INTO users (username, password, full_name, role, store_name, is_active)
         VALUES (?,?,?,?,?,1)""",
      (username, hash_password(password), full_name, role, store_name))
    return RedirectResponse(f"/manage-users?msg={uq(f'User {username} added.')}", status_code=303)


@router.post("/manage-users/save/{uid}")
def save_user(uid: int, session: str | None = Cookie(default=None),
              role: str = Form("staff"), store_name: str = Form("")):
    redir, user = _guard_owner(session)
    if redir:
        return redir
    if uid == user["user_id"]:
        return RedirectResponse(
            f"/manage-users?msg={uq('You cannot change your own account here.')}&msg_type=error",
            status_code=303)
    role       = role if role in ROLES else "staff"
    store_name = store_name if store_name in STORES else ""
    if role == "staff" and not store_name:
        return RedirectResponse(
            f"/manage-users?msg={uq('Staff users must have a store.')}&msg_type=error",
            status_code=303)
    q("UPDATE users SET role=?, store_name=? WHERE user_id=?", (role, store_name, uid))
    return RedirectResponse(f"/manage-users?msg={uq('User updated.')}", status_code=303)


@router.post("/manage-users/reset/{uid}")
def reset_password(uid: int, session: str | None = Cookie(default=None),
                   password: str = Form("")):
    redir, user = _guard_owner(session)
    if redir:
        return redir
    if not (password or "").strip():
        return RedirectResponse(
            f"/manage-users?msg={uq('Enter a new password.')}&msg_type=error", status_code=303)
    q("UPDATE users SET password=? WHERE user_id=?", (hash_password(password), uid))
    return RedirectResponse(f"/manage-users?msg={uq('Password reset.')}", status_code=303)


@router.post("/manage-users/toggle/{uid}")
def toggle_user(uid: int, session: str | None = Cookie(default=None)):
    redir, user = _guard_owner(session)
    if redir:
        return redir
    if uid == user["user_id"]:
        return RedirectResponse(
            f"/manage-users?msg={uq('You cannot disable your own account.')}&msg_type=error",
            status_code=303)
    row = q("SELECT is_active FROM users WHERE user_id=?", (uid,), fetch=True)
    if not row:
        return RedirectResponse(
            f"/manage-users?msg={uq('User not found.')}&msg_type=error", status_code=303)
    new = 0 if row[0]["is_active"] == 1 else 1
    q("UPDATE users SET is_active=? WHERE user_id=?", (new, uid))
    word = "enabled" if new else "disabled"
    return RedirectResponse(f"/manage-users?msg={uq(f'User {word}.')}", status_code=303)
