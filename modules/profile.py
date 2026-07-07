"""profile routes."""
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
from modules.staff import get_leave_summary

router = APIRouter()


@router.get("/my-profile", response_class=HTMLResponse)
def my_profile(session: str | None = Cookie(default=None), msg: str = "", msg_type: str = "success"):
    redir, user = require_login(session)
    if redir: return redir

    flash = ""
    if msg:
        cls = "flash-success" if msg_type == "success" else "flash-error"
        flash = f"<div class='{cls}'>{msg}</div>"

    # Change-your-own-password card — shown to every logged-in user, whether or
    # not they have a linked staff profile (so the owner can use it too).
    pw_card = """
    <div class='card'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:4px'>&#128273; Change Password</div>
      <div style='font-size:12px;color:#94a3b8;margin-bottom:16px'>
        Change your own password here. Choose something only you know (at least 6 characters).</div>
      <form action='/my-profile/password' method='POST' class='grid gap-3'
            style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr))'>
        <div><label>Current password</label><input type='password' name='current' required></div>
        <div><label>New password</label><input type='password' name='new' required minlength='6'></div>
        <div><label>Confirm new password</label><input type='password' name='confirm' required minlength='6'></div>
        <div style='grid-column:1/-1'><button type='submit' class='btn-primary'>Update password</button></div>
      </form>
    </div>"""

    # Find staff profile by matching full name to username
    full_name = user.get("full_name", "")
    rows = q("""SELECT * FROM staff_profiles
                WHERE first_name || ' ' || last_name = ?
                AND is_active = 1""", (full_name,), fetch=True)

    if not rows:
        content = f"""
        {flash}
        <div class='text-2xl font-black text-slate-800'>My Profile</div>
        <div class='card'>
          <p style='color:#64748b'>No staff profile linked to your account yet.
          Please contact your manager.</p>
        </div>
        {pw_card}"""
        return page("My Profile", content, user, "my profile")

    s     = dict(rows[0])
    sid   = s["staff_id"]
    year  = datetime.now().year
    leave = get_leave_summary(sid, year)

    content = f"""
    {flash}
    <div class='text-2xl font-black text-slate-800'>My Profile</div>

    <!-- Leave summary -->
    <div class='grid gap-4' style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr))'>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Leave Entitlement</div>
        <div style='font-size:24px;font-weight:900;color:#0f2942'>{leave.get("entitlement_fmt","—")}</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Holiday Taken</div>
        <div style='font-size:24px;font-weight:900;color:#d97706'>{leave.get("taken_days",0)} days</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Balance</div>
        <div style='font-size:24px;font-weight:900;color:#16a34a'>{leave.get("balance_fmt","—")}</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Sick Days {year}</div>
        <div style='font-size:24px;font-weight:900;color:{"#dc2626" if leave.get("sick_days",0) else "#0f2942"}'>{leave.get("sick_days",0)}</div>
      </div>
    </div>

    <!-- Editable personal details -->
    <div class='card'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:4px'>Personal Details</div>
      <div style='font-size:12px;color:#94a3b8;margin-bottom:16px'>
        You can update your contact details below. Employment details can only be changed by your manager.
      </div>
      <form action='/my-profile' method='POST' class='grid gap-3'
            style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>
        <div><label>Phone</label>
          <input type='text' name='phone' value='{s.get("phone") or ""}' placeholder='07700 123456'></div>
        <div><label>Email</label>
          <input type='email' name='email' value='{s.get("email") or ""}' placeholder='your@email.com'></div>
        <div><label>Address Line 1</label>
          <input type='text' name='address_1' value='{s.get("address_1") or ""}'></div>
        <div><label>Address Line 2</label>
          <input type='text' name='address_2' value='{s.get("address_2") or ""}'></div>
        <div><label>Town / City</label>
          <input type='text' name='address_3' value='{s.get("address_3") or ""}'></div>
        <div><label>Postcode</label>
          <input type='text' name='postcode' value='{s.get("postcode") or ""}'></div>
        <div style='grid-column:1/-1'>
          <button type='submit' class='btn-primary'>&#128190; Save Changes</button>
        </div>
      </form>
    </div>

    <!-- Read-only employment info — no pay rates shown to staff -->
    <div class='card'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Employment Details</div>
      <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr));font-size:13px'>
        <div><span style='color:#94a3b8;font-weight:700'>Store</span><br>{s.get("store_name") or "—"}</div>
        <div><span style='color:#94a3b8;font-weight:700'>Date Joined</span><br>{s.get("date_joined") or "—"}</div>
        <div><span style='color:#94a3b8;font-weight:700'>Contracted Hours</span><br>{str(s.get("contracted_hrs") or "—") + "h/wk"}</div>
      </div>
    </div>

    <!-- Leave request -->
    <div class='card'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>&#128197; Request Leave</div>
      <a href='/staff/{sid}/request-leave' class='btn-primary'>Submit Leave Request</a>
    </div>

    <!-- Change your own password -->
    {pw_card}"""

    return page("My Profile", content, user, "my profile")


@router.post("/my-profile")
async def save_my_profile(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir

    form      = await request.form()
    full_name = user.get("full_name","")

    q("""UPDATE staff_profiles SET
            phone=?, email=?, address_1=?, address_2=?, address_3=?, postcode=?
         WHERE first_name || ' ' || last_name = ? AND is_active=1""",
      (str(form.get("phone","") or "").strip(),
       str(form.get("email","") or "").strip(),
       str(form.get("address_1","") or "").strip(),
       str(form.get("address_2","") or "").strip(),
       str(form.get("address_3","") or "").strip(),
       str(form.get("postcode","") or "").strip(),
       full_name))

    from urllib.parse import quote as uq
    return RedirectResponse(f"/my-profile?msg={uq('Profile updated successfully')}", status_code=303)


@router.post("/my-profile/password")
async def change_my_password(request: Request, session: str | None = Cookie(default=None)):
    """Let any logged-in user change their OWN password (verifies the current
    one first). Owner resets others' passwords via Manage Users."""
    redir, user = require_login(session)
    if redir: return redir

    from urllib.parse import quote as uq
    form    = await request.form()
    current = str(form.get("current", "") or "")
    new     = str(form.get("new", "") or "")
    confirm = str(form.get("confirm", "") or "")

    if not verify_password(current, user.get("password", "")):
        return RedirectResponse(f"/my-profile?msg={uq('Current password is incorrect.')}&msg_type=error", status_code=303)
    if len(new) < 6:
        return RedirectResponse(f"/my-profile?msg={uq('New password must be at least 6 characters.')}&msg_type=error", status_code=303)
    if new != confirm:
        return RedirectResponse(f"/my-profile?msg={uq('New passwords do not match.')}&msg_type=error", status_code=303)

    q("UPDATE users SET password=? WHERE username=?", (hash_password(new), user["username"]))
    return RedirectResponse(f"/my-profile?msg={uq('Password updated.')}", status_code=303)
