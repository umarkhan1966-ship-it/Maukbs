"""staff routes."""
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
from docx import Document as DocxDocument
from docx.shared import Pt

router = APIRouter()


# ── Access-control helpers ──────────────────────────────────────────────────
# The Staff module holds sensitive personal/pay/tax data. Management routes are
# owner/manager only; self-service {staff_id} routes let a staff member reach
# ONLY their own record (owner/manager may reach anyone).
def _is_mgr(user) -> bool:
    return user.get("role") in ("owner", "manager")


def _require_mgr(user):
    """Bail (RedirectResponse) if the user isn't owner/manager, else None."""
    if not _is_mgr(user):
        return RedirectResponse("/?msg=That+area+is+for+managers+only&msg_type=error", status_code=303)
    return None


def _own_staff_id(user):
    """staff_id of the logged-in user's own profile (matched on full name), or None."""
    rows = q("SELECT staff_id FROM staff_profiles WHERE first_name||' '||last_name=? AND is_active=1",
             (user.get("full_name", ""),), fetch=True)
    return rows[0]["staff_id"] if rows else None


def _staff_access_guard(user, staff_id):
    """Self-service routes: owner/manager may access anyone; a staff user may
    access only their own record. Bail (RedirectResponse) otherwise, else None."""
    if _is_mgr(user):
        return None
    if _own_staff_id(user) == staff_id:
        return None
    return RedirectResponse("/my-profile?msg=You+can+only+access+your+own+record&msg_type=error",
                            status_code=303)


def _safe_part(s):
    """Filename-safe token — blocks path traversal via user-supplied name parts."""
    return (re.sub(r"[^A-Za-z0-9_-]", "_", str(s or ""))[:40]) or "x"


def _safe_ext(ext):
    """Whitelist upload extensions; unknown types become .dat (never executable)."""
    e = re.sub(r"[^a-z0-9.]", "", str(ext or "").lower())[:6]
    return e if e in (".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".webp") else ".dat"


def esc(x):
    """HTML-escape a value (guards stored XSS from user-entered fields like
    names/addresses that render into pages other users view)."""
    return html.escape(str(x), quote=True) if x is not None else ""


UK_BANK_HOLIDAYS_2026 = [
    "2026-01-01", "2026-04-03", "2026-04-06",
    "2026-05-04", "2026-05-25", "2026-08-31",
    "2026-12-25", "2026-12-28"
]


def calc_entitlement(contracted_hrs: float) -> float:
    """5.6 weeks × contracted hours per week, including bank holidays."""
    if not contracted_hrs:
        return 0.0
    return round(5.6 * contracted_hrs, 1)  # entitlement in hours


def hrs_to_days(hrs: float, contracted_hrs: float) -> float:
    """Convert hours to days based on contracted daily hours."""
    if not contracted_hrs or contracted_hrs == 0:
        return 0.0
    daily = contracted_hrs / 5
    return round(hrs / daily, 1) if daily > 0 else 0.0


def is_full_time(contracted_hrs: float) -> bool:
    return contracted_hrs is not None and contracted_hrs >= 30.0


def fmt_entitlement(hrs: float, contracted_hrs: float) -> str:
    """Format entitlement as days for FT (>=30h), hours for PT (<30h)."""
    if not contracted_hrs:
        return "—"
    if is_full_time(contracted_hrs):
        days = hrs_to_days(hrs, contracted_hrs)
        return f"{days} days"
    else:
        return f"{hrs} hrs ({hrs_to_days(hrs, contracted_hrs)} days)"


def get_leave_summary(staff_id: int, year: int = None) -> dict:
    """Return entitlement, taken, balance for a staff member."""
    if year is None:
        year = datetime.now().year
    staff = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not staff:
        return {}
    s = dict(staff[0])
    contracted = s.get("contracted_hrs") or 0

    # Check for custom entitlement first
    custom = q("""SELECT effective_days, statutory_days FROM leave_entitlements
                  WHERE staff_id=? AND year=?""", (staff_id, year), fetch=True)
    if custom:
        c = dict(custom[0])
        effective_days   = c["effective_days"] or c["statutory_days"] or 0
        entitlement_hrs  = effective_days * (contracted/5) if contracted else effective_days
    else:
        entitlement_hrs = calc_entitlement(contracted)

    # Days taken this year by type
    daily = contracted / 5 if contracted else 7.5

    taken_h = q("""SELECT COUNT(*) as n FROM leave_requests
                   WHERE staff_id=? AND status='approved'
                   AND leave_type='H' AND strftime('%Y',date_from)=?""",
                (staff_id, str(year)), fetch=True)
    holiday_days = taken_h[0]["n"] if taken_h else 0
    taken_hrs    = holiday_days * daily

    bh_used = q("""SELECT COUNT(*) as n FROM leave_requests
                   WHERE staff_id=? AND status='approved'
                   AND leave_type='B' AND strftime('%Y',date_from)=?""",
                (staff_id, str(year)), fetch=True)
    bh_days = bh_used[0]["n"] if bh_used else 0
    bh_hrs  = bh_days * daily

    sick_used = q("""SELECT COUNT(*) as n FROM leave_requests
                     WHERE staff_id=? AND status='approved'
                     AND leave_type='S' AND strftime('%Y',date_from)=?""",
                  (staff_id, str(year)), fetch=True)
    sick_days = sick_used[0]["n"] if sick_used else 0

    balance_hrs = entitlement_hrs - taken_hrs - bh_hrs
    # Sick days tracked separately — don't affect holiday balance
    daily = contracted / 5 if contracted else 7.5

    ft = is_full_time(contracted)
    return {
        "entitlement_hrs":  entitlement_hrs,
        "entitlement_days": hrs_to_days(entitlement_hrs, contracted),
        "entitlement_fmt":  fmt_entitlement(entitlement_hrs, contracted),
        "taken_hrs":        round(taken_hrs, 1),
        "taken_days":       holiday_days,
        "taken_fmt":        f"{holiday_days} days",
        "bh_days":          bh_days,
        "bh_hrs":           round(bh_hrs, 1),
        "sick_days":        sick_days,
        "balance_hrs":      round(balance_hrs, 1),
        "balance_days":     hrs_to_days(balance_hrs, contracted),
        "balance_fmt":      fmt_entitlement(round(balance_hrs,1), contracted),
        "daily_hrs":        round(daily, 2),
        "contracted_hrs":   contracted,
        "is_full_time":     ft,
    }


def ensure_staff_tables():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS leave_requests (
            request_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id      INTEGER NOT NULL,
            leave_type    TEXT NOT NULL DEFAULT 'H',
            date_from     TEXT NOT NULL,
            date_to       TEXT NOT NULL,
            days_taken    REAL DEFAULT 1,
            status        TEXT DEFAULT 'pending',
            requested_by  TEXT,
            approved_by   TEXT,
            approved_at   TEXT,
            notes         TEXT,
            created_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (staff_id) REFERENCES staff_profiles(staff_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS leave_entitlements (
            entitlement_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id         INTEGER NOT NULL,
            year             INTEGER NOT NULL,
            statutory_days   REAL,
            custom_days      REAL,
            effective_days   REAL,
            notes            TEXT,
            UNIQUE(staff_id, year),
            FOREIGN KEY (staff_id) REFERENCES staff_profiles(staff_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS document_templates (
            template_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type       TEXT NOT NULL,
            version        INTEGER DEFAULT 1,
            file_path      TEXT NOT NULL,
            file_name      TEXT,
            is_current     INTEGER DEFAULT 1,
            uploaded_by    TEXT,
            uploaded_at    TEXT DEFAULT (datetime('now')),
            notes          TEXT,
            UNIQUE(doc_type, version)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff_documents (
            doc_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id       INTEGER NOT NULL,
            doc_type       TEXT NOT NULL,
            version        INTEGER DEFAULT 1,
            file_path      TEXT NOT NULL,
            file_name      TEXT,
            is_current     INTEGER DEFAULT 1,
            generated      INTEGER DEFAULT 0,
            uploaded_by    TEXT,
            uploaded_at    TEXT DEFAULT (datetime('now')),
            notes          TEXT,
            FOREIGN KEY (staff_id) REFERENCES staff_profiles(staff_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pay_history (
            pay_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id      INTEGER NOT NULL,
            effective_date TEXT NOT NULL,
            hourly_rate   REAL NOT NULL,
            previous_rate REAL,
            change_reason TEXT,
            recorded_by   TEXT,
            created_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (staff_id) REFERENCES staff_profiles(staff_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS nmw_rates (
            nmw_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            effective_date TEXT NOT NULL,
            rate_21_plus  REAL,
            rate_18_20    REAL,
            rate_16_17    REAL,
            rate_apprentice REAL,
            UNIQUE(effective_date)
        )
    """)
    # Seed NMW rates from historical data
    nmw_data = [
        ("2026-04-01", 12.71, 10.85, 8.00,  7.55),
        ("2025-04-01", 12.21, 10.00, 7.55,  7.55),
        ("2024-04-01", 11.44,  8.60, 6.40,  6.40),
        ("2023-04-01", 10.42,  7.49, 5.28,  5.28),
        ("2022-04-01",  9.50,  6.83, 4.81,  4.81),
        ("2021-04-01",  8.91,  6.56, 4.62,  4.30),
        ("2020-04-01",  8.72,  6.45, 4.55,  4.15),
        ("2019-04-01",  8.21,  6.15, 4.35,  3.90),
        ("2018-04-01",  7.83,  5.90, 4.20,  3.70),
        ("2017-04-01",  7.50,  5.60, 4.05,  3.50),
    ]
    for row in nmw_data:
        try: c.execute("INSERT OR IGNORE INTO nmw_rates (effective_date,rate_21_plus,rate_18_20,rate_16_17,rate_apprentice) VALUES(?,?,?,?,?)", row)
        except: pass
    conn.commit()
    conn.close()


@router.get("/staff", response_class=HTMLResponse)
def staff_page(
    session:  str | None = Cookie(default=None),
    store:    str = "",
    show:     str = "active",
    msg:      str = "",
    msg_type: str = "success"
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    is_owner = user["role"] == "owner"

    # Build filter
    conds  = []
    params = []
    if show == "active":
        conds.append("is_active = 1")
    elif show == "leavers":
        if not is_owner:
            return RedirectResponse("/staff", status_code=303)
        conds.append("is_active = 0")
    # Store filter
    if store:
        conds.append("store_name = ?")
        params.append(store)
    elif user["role"] == "manager" and user.get("store_name"):
        conds.append("store_name = ?")
        params.append(user["store_name"])

    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    staff = q(f"SELECT * FROM staff_profiles {where} ORDER BY store_name, first_name",
              params, fetch=True) or []

    # Pending leave requests count
    pending_leave = q("""SELECT COUNT(*) as n FROM leave_requests lr
                         JOIN staff_profiles sp ON lr.staff_id=sp.staff_id
                         WHERE lr.status='pending'""", fetch=True)
    pending_n = pending_leave[0]["n"] if pending_leave else 0

    flash = f"<div class='flash-{'success' if msg_type=='success' else 'error'}'>{msg}</div>" if msg else ""

    # ── Tab bar ──
    tabs = ""
    for val, label in [("active","Active Staff"),("leavers","Former Staff (Leavers)")]:
        if val == "leavers" and not is_owner:
            continue
        active_cls = "border-b-2 border-blue-900 font-black text-blue-900" if show == val else "text-slate-500 hover:text-slate-700"
        tabs += f"<a href='/staff?show={val}' class='px-4 py-2 text-sm {active_cls} transition'>{label}</a>"

    # ── Store filter buttons ──
    store_btns = ""
    if is_owner or user["role"] == "manager":
        for sv, sl in [("","Both Stores"),("Uxbridge","Uxbridge"),("Newbury","Newbury")]:
            cls = "btn-primary" if store == sv else "btn-secondary"
            store_btns += f"<a href='/staff?show={show}&store={sv}' class='{cls}' style='padding:6px 14px;font-size:13px'>{sl}</a>"

    # ── Staff cards ──
    cards_html = ""
    year = datetime.now().year
    for s in staff:
        s = dict(s)
        sid   = s["staff_id"]
        name  = esc(f"{s['first_name']} {s['last_name']}")
        store_badge = f"<span style='background:#e0f2fe;color:#0369a1;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px'>{s.get('store_name','')}</span>"
        status_badge = "<span class='badge-paid'>Active</span>" if s["is_active"] else "<span class='badge-overdue'>Left</span>"

        # Quick leave summary
        leave = get_leave_summary(sid, year)
        bal      = leave.get("balance_days", 0)
        taken    = leave.get("taken_days", 0)
        entit    = leave.get("entitlement_days", 0)
        bal_fmt  = leave.get("balance_fmt", "—")
        tak_fmt  = leave.get("taken_fmt", "—")
        ent_fmt  = leave.get("entitlement_fmt", "—")
        bal_col  = "#16a34a" if bal > 5 else ("#d97706" if bal > 0 else "#dc2626")

        rate = f"£{s['hourly_rate']:.2f}/hr" if s.get("hourly_rate") else "—"
        hrs  = f"{s['contracted_hrs']}h/wk" if s.get("contracted_hrs") else "—"
        joined = s.get("date_joined") or "—"

        cards_html += f"""
        <div class='card' style='padding:0;overflow:hidden'>
          <div style='background:#f8fafc;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #e2e8f0'>
            <div>
              <div style='font-weight:900;font-size:15px;color:#0f172a'>{name}</div>
              <div style='display:flex;gap:6px;margin-top:4px'>{store_badge} {status_badge}</div>
            </div>
            <div style='display:flex;gap:8px'>
              <a href='/staff/{sid}' class='btn-primary' style='padding:5px 12px;font-size:12px'>👁 View</a>
              <a href='/staff/{sid}/edit' class='btn-secondary' style='padding:5px 12px;font-size:12px'>✏️ Edit</a>
            </div>
          </div>
          <div style='padding:12px 16px;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px'>
            <div><div style='font-size:11px;color:#94a3b8;font-weight:700;text-transform:uppercase'>Joined</div>
                 <div style='font-size:13px;font-weight:600;color:#334155'>{joined}</div></div>
            <div><div style='font-size:11px;color:#94a3b8;font-weight:700;text-transform:uppercase'>Hours</div>
                 <div style='font-size:13px;font-weight:600;color:#334155'>{hrs}</div></div>
            <div><div style='font-size:11px;color:#94a3b8;font-weight:700;text-transform:uppercase'>Rate</div>
                 <div style='font-size:13px;font-weight:600;color:#334155'>{rate}</div></div>
            <div><div style='font-size:11px;color:#94a3b8;font-weight:700;text-transform:uppercase'>Leave Balance {year}</div>
                 <div style='font-size:13px;font-weight:700;color:{bal_col}'>{bal_fmt} left <span style='color:#94a3b8;font-weight:400'>({tak_fmt} of {ent_fmt})</span></div></div>
          </div>
        </div>"""

    if not cards_html:
        cards_html = "<div class='card text-center' style='padding:40px;color:#94a3b8'>No staff found</div>"

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div class='text-2xl font-black text-slate-800'>👤 Staff</div>
      <div style='display:flex;gap:8px;flex-wrap:wrap'>
        {'<a href="/staff/leave-requests" class="btn-secondary" style="position:relative">📋 Leave Requests' + (f'<span style="position:absolute;top:-6px;right:-6px;background:#dc2626;color:white;border-radius:50%;width:18px;height:18px;font-size:10px;font-weight:900;display:flex;align-items:center;justify-content:center">{pending_n}</span>' if pending_n > 0 else '') + '</a>' if is_owner or user["role"]=="manager" else ''}
        <a href='/staff/leave-planner' class='btn-secondary'>📅 Leave Planner</a>
        {'<a href="/staff/pay-overview" class="btn-secondary">💰 Pay Overview</a>' if is_owner else ''}
        {'<a href="/staff/document-templates" class="btn-secondary">📋 Doc Templates</a>' if is_owner else ''}
        {'<a href="/staff/new" class="btn-primary">➕ Add Staff Member</a>' if is_owner or user["role"]=="manager" else ''}
      </div>
    </div>
    <div style='display:flex;gap:0;border-bottom:1px solid #e2e8f0'>{tabs}</div>
    <div style='display:flex;gap:8px;flex-wrap:wrap'>{store_btns}</div>
    <div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(420px,1fr))'>
      {cards_html}
    </div>"""

    return page("Staff", content, user, "staff")


@router.get("/staff/document-templates", response_class=HTMLResponse)
def document_templates(session: str | None = Cookie(default=None), msg: str = ""):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] != "owner":
        return RedirectResponse("/staff", status_code=303)

    templates = q("SELECT * FROM document_templates ORDER BY doc_type, version DESC",
                  fetch=True) or []

    flash = f"<div class='flash-success'>{msg}</div>" if msg else ""

    from collections import defaultdict
    by_type = defaultdict(list)
    for t in templates:
        by_type[dict(t)["doc_type"]].append(dict(t))

    tmpl_html = ""
    for dtype in DOC_TYPES:
        type_tmpls = by_type.get(dtype, [])
        current    = next((t for t in type_tmpls if t["is_current"]), None)
        older      = [t for t in type_tmpls if not t["is_current"]]

        current_html = ""
        if current:
            current_html = f"""
            <div style='background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:12px 14px'>
              <div style='font-size:13px;font-weight:700;color:#166534;margin-bottom:4px'>
                ✅ Current — v{current['version']} uploaded {current['uploaded_at'][:10]}
              </div>
              <div style='font-size:11px;color:#64748b;margin-bottom:10px'>{current.get('notes') or ''}</div>
              <div style='display:flex;gap:12px;align-items:center'>
                <a href='/staff/document-templates/{current["template_id"]}/download'
                   style='color:#64748b;font-size:12px;text-decoration:underline'>⬇️ download template</a>
                <a href='/staff/document-templates/{current["template_id"]}/delete'
                   onclick='return confirm("Delete this version? The previous version will become current.")'
                   class='btn-danger' style='padding:5px 14px;font-size:12px'>🗑️ Delete</a>
              </div>
            </div>"""

        older_html = "".join(
            f"<div style='font-size:12px;color:#94a3b8;padding:4px 10px'>v{t['version']} — {t['uploaded_at'][:10]} (superseded)</div>"
            for t in older
        )

        tmpl_html += f"""
        <div class='card'>
          <div style='font-weight:900;color:#0f2942;margin-bottom:8px'>{dtype}</div>
          {current_html or "<div style='color:#94a3b8;font-size:13px;padding:8px 0'>No template uploaded yet</div>"}
          {older_html}
          <form action='/staff/document-templates/upload' method='POST'
                enctype='multipart/form-data'
                style='margin-top:12px;padding-top:12px;border-top:1px solid #f1f5f9'
                onsubmit='showUploading(this)'>
            <input type='hidden' name='doc_type' value='{dtype}'>
            <div style='margin-bottom:8px'>
              <label style='font-size:11px;font-weight:700;color:#64748b;
                            text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px'>
                Select Template File (.docx or .dotx)
              </label>
              <input type='file' name='template_file' accept='.docx,.dotx' required
                     style='width:100%;border:1px solid #e2e8f0;border-radius:8px;
                            padding:8px 10px;font-size:13px;background:white;cursor:pointer'
                     onchange='previewFile(this)'>
              <div id='preview_{dtype.replace(" ","_")}' style='font-size:12px;color:#16a34a;
                   font-weight:700;margin-top:4px;display:none'>
                ✅ Selected: <span class='filename'></span>
              </div>
            </div>
            <div style='margin-bottom:8px'>
              <label style='font-size:11px;font-weight:700;color:#64748b;
                            text-transform:uppercase;letter-spacing:.05em;display:block;margin-bottom:4px'>
                Version Notes
              </label>
              <input type='text' name='notes' id='notes_{dtype.replace(" ","_")}'
                     placeholder='e.g. Updated Nov 2024 — new holiday clause'
                     style='width:100%;border:1px solid #e2e8f0;border-radius:8px;
                            padding:8px 10px;font-size:13px'>
            </div>
            <button type='submit' class='btn-primary' style='width:100%;padding:8px;font-size:13px'>
              ⬆️ Upload New Version
            </button>
          </form>
        </div>"""

    content = f"""
    {flash}
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>📋 Document Templates</div>
      <a href='/staff' class='btn-secondary'>← Back to Staff</a>
    </div>
    <div class='card' style='background:#fef3c7;border-color:#fcd34d'>
      <div style='font-size:13px;font-weight:700;color:#92400e'>📝 How to set up templates</div>
      <div style='font-size:13px;color:#78350f;margin-top:4px'>
        Create a Word (.docx) document with your letter/contract content.
        Use these placeholders where you want staff details inserted:
        <code style='background:#fff;padding:2px 6px;border-radius:4px;margin:0 4px'>{{{{FULL_NAME}}}}</code>
        <code style='background:#fff;padding:2px 6px;border-radius:4px;margin:0 4px'>{{{{STORE}}}}</code>
        <code style='background:#fff;padding:2px 6px;border-radius:4px;margin:0 4px'>{{{{DATE_JOINED}}}}</code>
        <code style='background:#fff;padding:2px 6px;border-radius:4px;margin:0 4px'>{{{{HOURLY_RATE}}}}</code>
        <code style='background:#fff;padding:2px 6px;border-radius:4px;margin:0 4px'>{{{{TODAY}}}}</code>
        and more. See the full list when generating a document.
      </div>
    </div>
    <div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(380px,1fr))'>
      {tmpl_html}
    </div>
    <script>
    function previewFile(input) {{
      if (!input.files.length) return;
      const fname = input.files[0].name;
      const form  = input.closest('form');
      // Show selected filename
      const preview = form.querySelector('[id^="preview_"]');
      if (preview) {{
        preview.querySelector('.filename').textContent = fname;
        preview.style.display = 'block';
      }}
      // Auto-fill notes with filename + date if empty
      const notes = form.querySelector('[id^="notes_"]');
      if (notes && !notes.value) {{
        const today = new Date().toLocaleDateString('en-GB', {{day:'2-digit',month:'short',year:'numeric'}});
        notes.value = fname + ' — uploaded ' + today;
      }}
    }}
    function showUploading(form) {{
      const btn = form.querySelector('button[type="submit"]');
      if (btn) {{ btn.textContent = '⏳ Uploading...'; btn.disabled = true; }}
    }}
    </script>"""

    return page("Document Templates", content, user, "staff")


@router.post("/staff/document-templates/upload")
async def upload_template(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] != "owner":
        return RedirectResponse("/staff/document-templates", status_code=303)

    form     = await request.form()
    doc_type = form.get("doc_type","")
    tmpl     = form.get("template_file")
    notes    = str(form.get("notes","") or "").strip()

    if not tmpl or not hasattr(tmpl,"filename") or not tmpl.filename:
        return RedirectResponse("/staff/document-templates?msg=No+file+selected", status_code=303)

    existing = q("SELECT MAX(version) as v FROM document_templates WHERE doc_type=?",
                 (doc_type,), fetch=True)
    next_ver = (existing[0]["v"] or 0) + 1 if existing else 1

    q("UPDATE document_templates SET is_current=0 WHERE doc_type=?", (doc_type,))

    filename = f"template_{_safe_part(doc_type)}_v{next_ver}.docx"
    filepath = os.path.join(TEMPLATES_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(await tmpl.read())

    q("""INSERT INTO document_templates
            (doc_type, version, file_path, file_name, is_current, uploaded_by, notes)
         VALUES(?,?,?,?,1,?,?)""",
      (doc_type, next_ver, filepath, tmpl.filename, user.get("username"), notes or None))

    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/staff/document-templates?msg={uq(doc_type + ' template uploaded (v' + str(next_ver) + ')')}",
        status_code=303)


@router.get("/staff/document-templates/{template_id}/delete")
def delete_template(template_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] != "owner":
        return RedirectResponse("/staff/document-templates", status_code=303)
    # Get the doc_type before deleting
    rows = q("SELECT * FROM document_templates WHERE template_id=?", (template_id,), fetch=True)
    if rows:
        t = dict(rows[0])
        # Delete the file from disk
        if os.path.exists(t["file_path"]):
            os.remove(t["file_path"])
        # Delete from database
        q("DELETE FROM document_templates WHERE template_id=?", (template_id,))
        # If this was current, make the previous version current
        q("""UPDATE document_templates SET is_current=1
             WHERE doc_type=? AND template_id=(
                SELECT MAX(template_id) FROM document_templates WHERE doc_type=?
             )""", (t["doc_type"], t["doc_type"]))
    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/staff/document-templates?msg={uq('Template version deleted')}",
        status_code=303)


@router.get("/staff/document-templates/{template_id}/download")
def download_template(template_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    rows = q("SELECT * FROM document_templates WHERE template_id=?", (template_id,), fetch=True)
    if not rows: return HTMLResponse("<p>Not found</p>", status_code=404)
    t = dict(rows[0])
    if not os.path.exists(t["file_path"]):
        return HTMLResponse("<p>File not found</p>", status_code=404)
    return FileResponse(t["file_path"], filename=t["file_name"] or os.path.basename(t["file_path"]),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.get("/staff/new", response_class=HTMLResponse)
def new_staff_form(session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner", "manager"):
        return RedirectResponse("/staff", status_code=303)
    return render_staff_form(user, None)


@router.get("/staff/leave-requests", response_class=HTMLResponse)
def leave_requests(session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse("/staff", status_code=303)

    pending = q("""
        SELECT lr.*, sp.first_name, sp.last_name, sp.store_name, sp.contracted_hrs
        FROM leave_requests lr
        JOIN staff_profiles sp ON lr.staff_id=sp.staff_id
        WHERE lr.status='pending'
        ORDER BY lr.date_from ASC
    """, fetch=True) or []

    recent = q("""
        SELECT lr.*, sp.first_name, sp.last_name, sp.store_name
        FROM leave_requests lr
        JOIN staff_profiles sp ON lr.staff_id=sp.staff_id
        WHERE lr.status != 'pending'
        ORDER BY lr.created_at DESC LIMIT 20
    """, fetch=True) or []

    def req_row(lr, show_actions=True):
        lr    = dict(lr)
        name  = f"{lr['first_name']} {lr['last_name']}"
        ltype = ABSENCE_TYPES.get(lr['leave_type'], lr['leave_type'])
        badge = {"approved":"<span class='badge-paid'>Approved</span>",
                 "pending": "<span class='badge-partial'>Pending</span>",
                 "declined":"<span class='badge-overdue'>Declined</span>"}.get(lr["status"],"")
        actions = ""
        if show_actions:
            actions = f"""
            <form method='POST' action='/staff/leave-requests/{lr['request_id']}/approve' style='display:inline'>
              <button type='submit' class='btn-success' style='padding:4px 10px;font-size:11px'>✅ Approve</button></form>
            <form method='POST' action='/staff/leave-requests/{lr['request_id']}/decline' style='display:inline'
                  onsubmit="return confirm('Decline this leave request?');">
              <button type='submit' class='btn-danger' style='padding:4px 10px;font-size:11px'>❌ Decline</button></form>"""
        return f"""<tr>
          <td style='font-weight:700'>{name}</td>
          <td style='font-size:12px;color:#64748b'>{lr.get('store_name','')}</td>
          <td>{ltype}</td>
          <td class='mono'>{lr['date_from']}</td>
          <td class='mono'>{lr['date_to']}</td>
          <td class='mono'>{lr['days_taken']}</td>
          <td>{badge}</td>
          <td style='font-size:12px;color:#64748b'>{lr.get('notes') or '—'}</td>
          <td><div style='display:flex;gap:4px'>{actions}</div></td>
        </tr>"""

    pending_rows = "".join(req_row(lr) for lr in pending)
    recent_rows  = "".join(req_row(lr, False) for lr in recent)

    content = f"""
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>📋 Leave Requests</div>
      <a href='/staff' class='btn-secondary'>← Back to Staff</a>
    </div>

    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:12px 16px;background:#d97706;color:white;font-weight:700;font-size:14px'>
        ⏳ Pending Approval ({len(pending)})
      </div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>Staff</th><th>Store</th><th>Type</th><th>From</th><th>To</th><th>Days</th><th>Status</th><th>Notes</th><th>Action</th></tr></thead>
          <tbody>{pending_rows or '<tr><td colspan="9" style="text-align:center;padding:24px;color:#94a3b8">No pending requests</td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:12px 16px;background:#0f2942;color:white;font-weight:700;font-size:14px'>Recent Decisions</div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>Staff</th><th>Store</th><th>Type</th><th>From</th><th>To</th><th>Days</th><th>Status</th><th>Notes</th><th></th></tr></thead>
          <tbody>{recent_rows or '<tr><td colspan="9" style="text-align:center;padding:24px;color:#94a3b8">No recent decisions</td></tr>'}</tbody>
        </table>
      </div>
    </div>"""
    return page("Leave Requests", content, user, "staff")


@router.get("/staff/leave-planner", response_class=HTMLResponse)
def leave_planner(
    session: str | None = Cookie(default=None),
    year:    int = 0,
    store:   str = ""
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    if not year: year = datetime.now().year

    # Get store filter
    if user["role"] == "manager" and user.get("store_name"):
        store = store or user["store_name"]

    # Get active staff
    conds  = ["is_active=1"]
    params = []
    if store:
        conds.append("store_name=?")
        params.append(store)
    staff = q(f"SELECT * FROM staff_profiles WHERE {' AND '.join(conds)} ORDER BY first_name",
              params, fetch=True) or []

    # Get all approved leave for this year
    leave_data = q("""
        SELECT lr.staff_id, lr.date_from, lr.date_to, lr.leave_type
        FROM leave_requests lr
        WHERE lr.status='approved'
          AND strftime('%Y',lr.date_from)=?
    """, (str(year),), fetch=True) or []

    # Build a set of (staff_id, date) → leave_type
    leave_map = {}
    for lr in leave_data:
        lr = dict(lr)
        try:
            d1 = datetime.strptime(lr["date_from"], "%Y-%m-%d")
            d2 = datetime.strptime(lr["date_to"],   "%Y-%m-%d")
            cur = d1
            while cur <= d2:
                leave_map[(lr["staff_id"], cur.strftime("%Y-%m-%d"))] = lr["leave_type"]
                cur = cur.replace(day=cur.day+1) if cur.day < 28 else cur.replace(
                    month=cur.month+1 if cur.month<12 else 1,
                    year=cur.year+1 if cur.month==12 else cur.year, day=1)
        except: pass

    # BH set
    bh_set = set(UK_BANK_HOLIDAYS_2026)

    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    import calendar

    # Build the planner grid
    grid_html = ""
    for mi, mname in enumerate(months, 1):
        _, days_in_month = calendar.monthrange(year, mi)
        # Header row
        grid_html += f"<tr><td style='background:#0f2942;color:white;font-weight:900;font-size:12px;padding:6px 10px;white-space:nowrap'>{mname}</td>"
        for d in range(1, 32):
            if d > days_in_month:
                grid_html += "<td style='background:#f8fafc'></td>"
                continue
            date_str = f"{year}-{mi:02d}-{d:02d}"
            dow = datetime(year, mi, d).weekday()
            is_weekend = dow >= 5
            is_bh = date_str in bh_set
            if is_bh:
                bg = "#fef3c7"; txt = "<span style='font-size:9px;color:#92400e;font-weight:700'>BH</span>"
            elif is_weekend:
                bg = "#f1f5f9"; txt = ""
            else:
                bg = "white"; txt = ""
            grid_html += f"<td style='background:{bg};border:1px solid #e2e8f0;text-align:center;padding:2px;min-width:28px;font-size:10px'>{txt}</td>"
        grid_html += "</tr>"

        # Staff rows for this month
        for s in staff:
            s = dict(s)
            sid   = s["staff_id"]
            # First name or nickname (first 3 chars)
            initials = s["first_name"][:3]
            grid_html += f"<tr><td style='font-size:11px;font-weight:700;color:#334155;padding:3px 10px;white-space:nowrap;border-bottom:1px solid #f1f5f9'>{initials}</td>"
            for d in range(1, 32):
                if d > days_in_month:
                    grid_html += "<td style='background:#f8fafc'></td>"
                    continue
                date_str = f"{year}-{mi:02d}-{d:02d}"
                dow = datetime(year, mi, d).weekday()
                is_weekend = dow >= 5
                ltype = leave_map.get((sid, date_str))
                if ltype == "H":
                    bg = "#dcfce7"; cell = f"<span style='font-size:9px;font-weight:900;color:#166534'>{initials}</span>"
                elif ltype == "S":
                    bg = "#fee2e2"; cell = f"<span style='font-size:9px;font-weight:900;color:#991b1b'>S</span>"
                elif ltype == "B":
                    bg = "#fef3c7"; cell = ""
                elif is_weekend:
                    bg = "#f1f5f9"; cell = ""
                else:
                    bg = "white"; cell = ""
                grid_html += f"<td style='background:{bg};border:1px solid #f1f5f9;text-align:center;padding:1px;font-size:9px'>{cell}</td>"
            grid_html += "</tr>"
        # Spacer between months
        grid_html += f"<tr><td colspan='32' style='height:4px;background:#f8fafc'></td></tr>"

    # Store filter buttons
    store_btns = ""
    if user["role"] in ("owner","manager"):
        for sv,sl in [("","Both"),("Uxbridge","Uxbridge"),("Newbury","Newbury")]:
            cls = "btn-primary" if store==sv else "btn-secondary"
            store_btns += f"<a href='/staff/leave-planner?year={year}&store={sv}' class='{cls}' style='padding:5px 12px;font-size:12px'>{sl}</a>"

    # Legend
    legend = """
    <div style='display:flex;gap:12px;flex-wrap:wrap;font-size:12px;font-weight:600'>
      <span><span style='display:inline-block;width:14px;height:14px;background:#dcfce7;border:1px solid #86efac;border-radius:3px;vertical-align:middle'></span> Holiday</span>
      <span><span style='display:inline-block;width:14px;height:14px;background:#fee2e2;border:1px solid #fca5a5;border-radius:3px;vertical-align:middle'></span> Sick</span>
      <span><span style='display:inline-block;width:14px;height:14px;background:#fef3c7;border:1px solid #fcd34d;border-radius:3px;vertical-align:middle'></span> Bank Holiday</span>
      <span><span style='display:inline-block;width:14px;height:14px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:3px;vertical-align:middle'></span> Weekend</span>
    </div>"""

    content = f"""
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div class='text-2xl font-black text-slate-800'>📅 Leave Planner {year}</div>
      <div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>
        {store_btns}
        <a href='/staff/leave-planner?year={year-1}&store={store}' class='btn-secondary' style='padding:5px 12px;font-size:12px'>← {year-1}</a>
        <a href='/staff/leave-planner?year={year+1}&store={store}' class='btn-secondary' style='padding:5px 12px;font-size:12px'>{year+1} →</a>
        <a href='/staff' class='btn-secondary' style='padding:5px 12px;font-size:12px'>← Staff</a>
      </div>
    </div>
    {legend}
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='overflow-x:auto'>
        <table style='border-collapse:collapse;font-family:DM Mono,monospace;width:100%'>
          <!-- Day numbers header -->
          <tr>
            <td style='background:#0f2942;color:white;font-size:11px;font-weight:700;padding:6px 10px;white-space:nowrap'>Month / Day</td>
            {"".join(f"<td style='background:#0f2942;color:white;font-size:10px;font-weight:700;text-align:center;padding:3px;min-width:28px'>{d}</td>" for d in range(1,32))}
          </tr>
          {grid_html}
        </table>
      </div>
    </div>"""
    return page("Leave Planner", content, user, "staff")


def get_nmw_for_person(dob_str: str, check_date: str = None) -> float:
    """Return current NMW rate for a person based on their age."""
    if not dob_str: return 0.0
    if not check_date: check_date = datetime.now().strftime("%Y-%m-%d")
    try:
        dob  = datetime.strptime(dob_str, "%Y-%m-%d")
        chk  = datetime.strptime(check_date, "%Y-%m-%d")
        age  = (chk - dob).days // 365
        # Get most recent NMW rates effective on or before check_date
        rates = q("""SELECT * FROM nmw_rates WHERE effective_date <= ?
                     ORDER BY effective_date DESC LIMIT 1""",
                  (check_date,), fetch=True)
        if not rates: return 0.0
        r = dict(rates[0])
        if age >= 21: return r["rate_21_plus"]
        elif age >= 18: return r["rate_18_20"]
        else: return r["rate_16_17"]
    except: return 0.0


@router.get("/staff/pay-overview", response_class=HTMLResponse)
def pay_overview(session: str | None = Cookie(default=None)):
    """Owner-only overview of all staff pay vs NMW."""
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] != "owner":
        return RedirectResponse("/staff", status_code=303)

    today = datetime.now().strftime("%Y-%m-%d")
    staff = q("SELECT * FROM staff_profiles WHERE is_active=1 ORDER BY store_name, first_name",
              fetch=True) or []

    rows_html = ""
    warnings  = 0
    for s in staff:
        s       = dict(s)
        current = s.get("hourly_rate") or 0
        nmw     = get_nmw_for_person(s.get("date_of_birth",""), today)
        diff    = round(current - nmw, 2) if nmw else 0
        annual  = current * (s.get("contracted_hrs") or 0) * 52

        if nmw == 0:
            status = "<span class='badge-unpaid'>No DOB</span>"
        elif diff < 0:
            status = f"<span class='badge-overdue'>⚠️ £{abs(diff):.2f} BELOW</span>"
            warnings += 1
        elif diff < 0.50:
            status = f"<span class='badge-partial'>Near NMW +£{diff:.2f}</span>"
        else:
            status = f"<span class='badge-paid'>✅ +£{diff:.2f}</span>"

        dob = s.get("date_of_birth","")
        age = ((datetime.now() - datetime.strptime(dob,"%Y-%m-%d")).days//365) if dob else "?"
        rows_html += f"""<tr>
          <td style='font-weight:700'>{esc(s['first_name'])} {esc(s['last_name'])}</td>
          <td style='font-size:12px;color:#64748b'>{esc(s.get('store_name',''))}</td>
          <td>{age}</td>
          <td class='mono' style='font-weight:700'>£{current:.2f}</td>
          <td class='mono' style='color:#64748b'>£{nmw:.2f}</td>
          <td>{status}</td>
          <td class='mono'>£{annual:,.0f}</td>
          <td><a href='/staff/{s["staff_id"]}/pay-history' class='btn-secondary' style='padding:3px 10px;font-size:11px'>History</a></td>
        </tr>"""

    content = f"""
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div class='text-2xl font-black text-slate-800'>💰 Pay Overview — All Staff</div>
      <a href='/staff' class='btn-secondary'>← Back to Staff</a>
    </div>
    {'<div class="flash-error">⚠️ ' + str(warnings) + ' staff member(s) may be below National Minimum Wage — please review</div>' if warnings else ''}
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>Name</th><th>Store</th><th>Age</th><th>Current Rate</th><th>NMW</th><th>Status</th><th>Annual Equiv.</th><th></th></tr></thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>"""
    return page("Pay Overview", content, user, "staff")


from core.paths import data_path
DOCS_DIR      = data_path("staff_docs")   # generated docs → persistent volume


TEMPLATES_DIR = "doc_templates"           # shipped assets → stay next to the code


DOC_TYPES = [
    "Offer Letter",
    "Employment Contract",
    "Right to Work",
    "P45/P46",
    "New Employee Notification",
    "DBS Check",
    "Other",
]


def get_store_entity(store_name: str) -> dict:
    """The legal entity a store trades as, read from the store_entities table
    (never hardcoded) so a future change — e.g. the Uxbridge LLP being replaced
    by its Ltd partner — is a one-row edit that every generated document picks up."""
    rows = q("SELECT * FROM store_entities WHERE store_name=?", (store_name or "",), fetch=True)
    if rows:
        return dict(rows[0])
    # Fallback keeps generation working even if a store has no entity row yet
    return {"store_name": store_name, "legal_name": store_name or "the Company",
            "trading_name": "", "addr_line1": "", "addr_line2": "",
            "addr_line3": "", "addr_line4": ""}


def get_merge_fields(staff: dict) -> dict:
    """Return all merge fields for Word template substitution.
    Supports both <<field>> (your existing format) and {{FIELD}} formats.
    """
    today    = datetime.now().strftime("%d %B %Y")
    name     = f"{staff.get('first_name','')} {staff.get('last_name','')}".strip()
    store    = staff.get('store_name','')
    ent      = get_store_entity(store)
    employer      = (f"{ent['legal_name']} T/A {ent['trading_name']}"
                     if ent.get('trading_name') else ent['legal_name'])
    store_lines   = [ent.get('addr_line1'), ent.get('addr_line2'),
                     ent.get('addr_line3'), ent.get('addr_line4')]
    store_addr    = ", ".join(x for x in store_lines if x)   # postal address, one line
    employer_addr = f"{employer}, {store_addr}" if store_addr else employer
    hrs      = staff.get('contracted_hrs') or 0
    rate     = staff.get('hourly_rate') or 0
    emp_type = staff.get('employment_type') or ('Full-time' if hrs >= 30 else 'Part-time')
    pay_type = 'salary' if staff.get('is_salaried') == 'Y' else 'hourly rate of'
    wages    = f"£{staff.get('salary_amount',0):,.2f} per annum" if staff.get('is_salaried') == 'Y' else f"£{rate:.2f} per hour"
    job_title  = staff.get('job_title') or 'Sales Assistant'
    reports_to = staff.get('reports_to') or 'Store Manager'

    # Support both << >> and {{ }} formats
    fields = {}

    # Your existing << >> format
    angle = {
        "<<employee name>>":           name,
        "<<employee first name>>":     staff.get('first_name',''),
        "<<address line 1>>":          staff.get('address_1','') or '',
        "<<address line 2>>":          staff.get('address_2','') or '',
        "<<address line 3>>":          staff.get('address_3','') or '',
        "<<address line 4>>":          staff.get('address_4','') or '',
        "<<post code>>":               staff.get('postcode','') or '',
        "<<today's date>>":            today,
        "<<today’s date>>":       today,   # templates use a curly apostrophe
        "<<position>>":                job_title,
        "<<Position>>":                job_title,   # offer-letter template capitalises it
        "<<FT or PT>>":                emp_type,
        "<<salary or hourly>>":        pay_type,
        "<<wages>>":                   wages,
        "<<employer>>":                employer,
        "<<employer and store address>>": employer_addr,
        "<<store address>>":           store_addr,
        "<<s tore address >>":         store_addr,
        "<<store address line 1>>":    ent.get('addr_line1','') or '',
        "<<store address line 2>>":    ent.get('addr_line2','') or '',
        "<<store address line 3>>":    ent.get('addr_line3','') or '',
        "<<store address line 4>>":    ent.get('addr_line4','') or '',
        "<<reporting to>>":            reports_to,
        "<<contracted hours>>":        f"{hrs} hours per week",
        "<<hours of work>>":           f"{hrs} hours per week",   # contract template token
        "<<hourly rate>>":             f"£{rate:.2f}",
        "<<date of joining>>":         staff.get('date_joined','') or '',
        "<<DOJ>>":                     staff.get('date_joined','') or '',   # contract template token
        "<<date of birth>>":           staff.get('date_of_birth','') or '',
        "<<p osition>>":               job_title,
        "<<e mployer>>":               employer,
    }
    fields.update(angle)

    # Also {{ }} format for new templates
    curly = {
        "{{FULL_NAME}}":        name,
        "{{FIRST_NAME}}":       staff.get('first_name',''),
        "{{LAST_NAME}}":        staff.get('last_name',''),
        "{{ADDRESS_1}}":        staff.get('address_1','') or '',
        "{{ADDRESS_2}}":        staff.get('address_2','') or '',
        "{{ADDRESS_3}}":        staff.get('address_3','') or '',
        "{{POSTCODE}}":         staff.get('postcode','') or '',
        "{{EMAIL}}":            staff.get('email','') or '',
        "{{PHONE}}":            staff.get('phone','') or '',
        "{{STORE}}":            store,
        "{{STORE_ADDRESS}}":    store_addr,
        "{{DATE_JOINED}}":      staff.get('date_joined','') or '',
        "{{DATE_OF_BIRTH}}":    staff.get('date_of_birth','') or '',
        "{{CONTRACTED_HOURS}}": str(hrs),
        "{{HOURLY_RATE}}":      f"£{rate:.2f}" if rate else '',
        "{{JOB_TITLE}}":        job_title,
        "{{REPORTS_TO}}":       reports_to,
        "{{EMP_TYPE}}":         emp_type,
        "{{TODAY}}":            today,
        "{{YEAR}}":             str(datetime.now().year),
        "{{EMPLOYER}}":         "Maukbs Ltd T/A Snappy Snaps",
    }
    fields.update(curly)
    return fields


def fill_word_template(template_path: str, fields: dict) -> bytes:
    """Fill a Word .docx/.dotx template with merge fields and return as bytes.
    Handles split runs where a merge field spans multiple runs in the same paragraph.
    """
    # For .dotx files, copy to a temp .docx first
    import tempfile, shutil
    if template_path.lower().endswith('.dotx'):
        tmp = tempfile.NamedTemporaryFile(suffix='.docx', delete=False)
        shutil.copy(template_path, tmp.name)
        doc = DocxDocument(tmp.name)
        os.unlink(tmp.name)
    else:
        doc = DocxDocument(template_path)

    def replace_para_text(para):
        """Replace merge fields even when split across runs."""
        # First try simple per-run replacement
        for run in para.runs:
            for key, val in fields.items():
                if key in run.text:
                    run.text = run.text.replace(key, val)

        # Then handle split runs by rebuilding full paragraph text
        full_text = para.text
        changed = False
        for key, val in fields.items():
            if key in full_text:
                full_text = full_text.replace(key, val)
                changed = True

        if changed:
            # Put all text in first run, clear others
            if para.runs:
                para.runs[0].text = full_text
                for run in para.runs[1:]:
                    run.text = ''

    for para in doc.paragraphs:
        replace_para_text(para)

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    replace_para_text(para)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@router.get("/staff/{staff_id}", response_class=HTMLResponse)
def staff_profile(staff_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir

    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows:
        return RedirectResponse("/staff", status_code=303)
    s = dict(rows[0])

    # Access control — staff can only see their own profile
    if user["role"] == "staff":
        my_rows = q("SELECT staff_id FROM staff_profiles WHERE first_name||' '||last_name=?",
                    (user.get("full_name",""),), fetch=True)
        my_id = my_rows[0]["staff_id"] if my_rows else None
        if my_id != staff_id:
            return RedirectResponse(f"/staff/{my_id}" if my_id else "/", status_code=303)

    is_leaver = not s["is_active"]
    if is_leaver and user["role"] != "owner":
        return RedirectResponse("/staff", status_code=303)

    year  = datetime.now().year
    leave = get_leave_summary(staff_id, year)

    # Leave history
    leave_hist = q("""
        SELECT * FROM leave_requests
        WHERE staff_id=? ORDER BY date_from DESC LIMIT 20
    """, (staff_id,), fetch=True) or []

    # Recent attendance
    attendance = q("""
        SELECT * FROM timesheets WHERE staff_name=?
        ORDER BY work_date DESC LIMIT 10
    """, (f"{s['first_name']} {s['last_name']}",), fetch=True) or []

    name = esc(f"{s['first_name']} {s['last_name']}")

    # ── Leave history table ──
    leave_rows = ""
    for lr in leave_hist:
        lr = dict(lr)
        at = ABSENCE_TYPES.get(lr["leave_type"], lr["leave_type"])
        status_badge = {
            "approved": "<span class='badge-paid'>Approved</span>",
            "pending":  "<span class='badge-partial'>Pending</span>",
            "declined": "<span class='badge-overdue'>Declined</span>",
        }.get(lr["status"], lr["status"])
        leave_rows += f"""
        <tr>
          <td>{lr['date_from']}</td>
          <td>{lr['date_to']}</td>
          <td>{at}</td>
          <td class='mono'>{lr['days_taken']}</td>
          <td>{status_badge}</td>
          <td style='font-size:12px;color:#64748b'>{lr.get('notes') or '—'}</td>
        </tr>"""

    # ── Attendance table ──
    att_rows = ""
    for a in attendance:
        a = dict(a)
        ci = a.get("clock_in_time") or "—"
        co = a.get("clock_out_time") or "⚠️ Open"
        att_rows += f"<tr><td class='mono'>{a['work_date']}</td><td class='mono' style='color:#16a34a'>{ci}</td><td class='mono' style='color:#dc2626'>{co}</td><td><span class='badge-{'paid' if a.get('status_flag')=='GPS_VERIFIED' else 'unpaid'}'>{a.get('status_flag') or '—'}</span></td></tr>"

    can_edit = user["role"] in ("owner", "manager")

    content = f"""
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <a href='/staff' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to Staff</a>
        <div class='text-2xl font-black text-slate-800 mt-1'>{name}</div>
        <div style='color:#64748b;font-size:13px'>{s.get('store_name') or ''} {'· Left ' + s['date_left'] if is_leaver and s.get('date_left') else ''}</div>
      </div>
      <div style='display:flex;gap:8px;flex-wrap:wrap'>
        {'<a href="/staff/' + str(staff_id) + '/edit" class="btn-primary">✏️ Edit Profile</a>' if can_edit else ''}
        <a href='/staff/{staff_id}/request-leave' class='btn-secondary'>📅 Request Leave</a>
        {'<a href="/staff/' + str(staff_id) + '/pay-history" class="btn-secondary">💰 Pay History</a>' if can_edit else ''}
        {'<a href="/staff/' + str(staff_id) + '/set-entitlement" class="btn-secondary">⚙️ Set Entitlement</a>' if can_edit else ''}
        <a href='/staff/{staff_id}/documents' class='btn-secondary'>&#128193; Documents</a>
        <a href='/staff/{staff_id}/onboarding' class='btn-secondary'>&#128203; Onboarding</a>
      </div>
    </div>

    <!-- Summary cards -->
    <div class='grid gap-4' style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr))'>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Entitlement {year}</div>
        <div style='font-size:20px;font-weight:900;color:#0f2942'>{leave.get("entitlement_fmt","—")}</div>
        <div style='font-size:10px;color:#94a3b8'>inc. bank holidays</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Holiday Taken</div>
        <div style='font-size:20px;font-weight:900;color:#d97706'>{leave.get("taken_days",0)} days</div>
        <div style='font-size:10px;color:#94a3b8'>{leave.get("bh_days",0)} bank hols used</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Holiday Balance</div>
        <div style='font-size:20px;font-weight:900;color:{"#16a34a" if leave.get("balance_days",0)>0 else "#dc2626"}'>{leave.get("balance_fmt","—")}</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Sick Days {year}</div>
        <div style='font-size:20px;font-weight:900;color:{"#dc2626" if leave.get("sick_days",0)>0 else "#0f2942"}'>{leave.get("sick_days",0)}</div>
        <div style='font-size:10px;color:#94a3b8'>does not affect holiday</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Contract</div>
        <div style='font-size:24px;font-weight:900;color:#0f2942'>{s.get('contracted_hrs') or '—'}</div>
        <div style='font-size:11px;color:#94a3b8'>hrs/week</div>
      </div>
    </div>

    <!-- Personal details -->
    <div class='card'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Personal Details</div>
      <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr));font-size:13px'>
        <div><span style='color:#94a3b8;font-weight:700'>Date of Birth</span><br>{s.get('date_of_birth') or '—'}</div>
        <div><span style='color:#94a3b8;font-weight:700'>Phone</span><br>{esc(s.get('phone')) or '—'}</div>
        <div><span style='color:#94a3b8;font-weight:700'>Email</span><br>{esc(s.get('email')) or '—'}</div>
        <div><span style='color:#94a3b8;font-weight:700'>Address</span><br>{esc(', '.join(filter(None,[s.get('address_1'),s.get('address_2'),s.get('address_3'),s.get('postcode')]))) or '—'}</div>
        <div><span style='color:#94a3b8;font-weight:700'>Date Joined</span><br>{s.get('date_joined') or '—'}</div>
        <div><span style='color:#94a3b8;font-weight:700'>Hourly Rate</span><br>{'£'+str(s['hourly_rate'])+'/hr' if s.get('hourly_rate') else '—'}</div>
      </div>
    </div>


        <!-- Leave history -->
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:12px 16px;background:#0f2942;color:white;font-weight:700;font-size:14px;display:flex;justify-content:space-between;align-items:center'>
        <span>📅 Leave History {year}</span>
        <a href='/staff/{staff_id}/request-leave' style='background:rgba(255,255,255,.15);color:white;font-size:12px;font-weight:700;padding:4px 12px;border-radius:6px;text-decoration:none'>+ Request Leave</a>
      </div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>From</th><th>To</th><th>Type</th><th>Days</th><th>Status</th><th>Notes</th></tr></thead>
          <tbody>{leave_rows or '<tr><td colspan="6" style="text-align:center;padding:24px;color:#94a3b8">No leave recorded this year</td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <!-- Attendance -->
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:12px 16px;background:#0f2942;color:white;font-weight:700;font-size:14px'>⏱ Recent Attendance</div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>Date</th><th>Clock In</th><th>Clock Out</th><th>Status</th></tr></thead>
          <tbody>{att_rows or '<tr><td colspan="4" style="text-align:center;padding:24px;color:#94a3b8">No attendance records</td></tr>'}</tbody>
        </table>
      </div>
    </div>"""

    return page(name, content, user, "staff")


@router.get("/staff/{staff_id}/edit", response_class=HTMLResponse)
def edit_staff_form(staff_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows:
        return RedirectResponse("/staff", status_code=303)
    s = dict(rows[0])
    # Staff can only edit their own profile (limited fields)
    if user["role"] == "staff":
        my_rows = q("SELECT staff_id FROM staff_profiles WHERE first_name||' '||last_name=?",
                    (user.get("full_name",""),), fetch=True)
        my_id = my_rows[0]["staff_id"] if my_rows else None
        if my_id != staff_id:
            return RedirectResponse("/", status_code=303)
    return render_staff_form(user, s)


def render_staff_form(user: dict, s: dict | None) -> HTMLResponse:
    is_edit   = s is not None
    is_owner  = user["role"] == "owner"
    is_mgr    = user["role"] in ("owner", "manager")
    is_self   = user["role"] == "staff"
    title     = f"✏️ Edit — {s['first_name']} {s['last_name']}" if is_edit else "➕ New Staff Member"
    action    = f"/staff/{s['staff_id']}/save" if is_edit else "/staff/save"
    back_url  = f"/staff/{s['staff_id']}" if is_edit else "/staff"
    sv        = s or {}

    def fi(name, label, ftype="text", val=None, req=False, opts=None, disabled=False, placeholder=""):
        safe = val if val is not None else ""
        req_a = "required" if req else ""
        dis_a = "disabled style='background:#f8fafc;color:#94a3b8'" if disabled else ""
        step  = "step='0.01'" if ftype=="number" else ""
        ph    = f"placeholder='{placeholder}'" if placeholder else ""
        if opts is not None:
            o = "".join(f"<option value='{ov}' {'selected' if str(safe)==str(ov) else ''}>{ol}</option>"
                        for ov,ol in opts)
            return f"<div><label>{label}</label><select name='{name}' {req_a} {dis_a}>{o}</select></div>"
        return f"<div><label>{label}</label><input type='{ftype}' name='{name}' value='{esc(safe)}' {req_a} {dis_a} {step} {ph}></div>"

    store_opts = [("","-- Select --"),("Uxbridge","Uxbridge"),("Newbury","Newbury")]
    sex_opts   = [("","--"),("M","Male"),("F","Female"),("O","Other")]
    active_opts= [("1","Active"),("0","Left / Leaver")]

    # Personal details — staff can edit these themselves
    personal = f"""
    <div class='card'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Personal Details</div>
      <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr))'>
        {fi('first_name','First Name','text',sv.get('first_name'),req=True,disabled=is_self)}
        {fi('last_name', 'Last Name', 'text',sv.get('last_name'), req=True,disabled=is_self)}
        {fi('date_of_birth','Date of Birth','date',sv.get('date_of_birth'))}
        {fi('sex','Gender',opts=sex_opts,val=sv.get('sex'))}
        {fi('phone','Phone','text',sv.get('phone'),placeholder='07700 000000')}
        {fi('email','Email','email',sv.get('email'))}
        {fi('address_1','Address Line 1','text',sv.get('address_1'))}
        {fi('address_2','Address Line 2','text',sv.get('address_2'))}
        {fi('address_3','Town/City','text',sv.get('address_3'))}
        {fi('postcode','Postcode','text',sv.get('postcode'))}
      </div>
    </div>"""

    # Employment details — manager/owner only
    employment = ""
    if is_mgr:
        employment = f"""
    <div class='card'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Employment Details</div>
      <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr))'>
        {fi('staff_number','Staff Number','number',sv.get('staff_number'))}
        {fi('store_name','Store',opts=store_opts,val=sv.get('store_name'),req=True)}
        {fi('job_title','Job Title','text',sv.get('job_title',''),placeholder='e.g. Sales Assistant')}
        {fi('employment_type','Employment Type',opts=[('Full-time','Full-time'),('Part-time','Part-time')],val=sv.get('employment_type','Part-time'))}
        {fi('reports_to','Reports To','text',sv.get('reports_to',''),placeholder='e.g. Store Manager')}
        {fi('date_joined','Date Joined','date',sv.get('date_joined'))}
        {fi('contracted_hrs','Contracted Hours/Week','number',sv.get('contracted_hrs'),placeholder='e.g. 37.5')}
        {fi('hourly_rate','Hourly Rate (£)','number',sv.get('hourly_rate'),placeholder='e.g. 11.44')}
        {fi('is_salaried','Salaried?',opts=[('N','No'),('Y','Yes')],val=sv.get('is_salaried','N'))}
        {fi('salary_amount','Salary Amount (£/yr)','number',sv.get('salary_amount'))}
        {fi('is_active','Status',opts=active_opts,val=str(sv.get('is_active',1)))}
        {fi('date_left','Date Left','date',sv.get('date_left')) if is_edit else ''}
        {fi('leaving_reason','Reason for Leaving','text',sv.get('leaving_reason')) if is_edit else ''}
      </div>
    </div>"""

    content = f"""
    <div class='flex justify-between items-center'>
      <div>
        <a href='{back_url}' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back</a>
        <div class='text-2xl font-black text-slate-800 mt-1'>{title}</div>
      </div>
    </div>
    <form action='{action}' method='POST' enctype='multipart/form-data'>
      {personal}
      {employment}
      <div class='card'>
        <div style='display:flex;gap:8px'>
          <button type='submit' class='btn-primary'>{'💾 Save Changes' if is_edit else '➕ Add Staff Member'}</button>
          <a href='{back_url}' class='btn-secondary'>Cancel</a>
          {'<a href="/staff/' + str(s["staff_id"]) + '/delete" class="btn-danger" onclick="return confirm(\'Are you sure?\')">🗑️ Delete</a>' if is_edit and is_owner else ''}
        </div>
      </div>
    </form>"""

    return HTMLResponse(page(title, content, user, "staff"))


@router.post("/staff/save")
async def save_new_staff(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    form = await request.form()
    fv = lambda k, d="": str(form.get(k, d) or d).strip()
    fn = lambda k: float(form.get(k, 0) or 0) if form.get(k) else None
    q("""INSERT INTO staff_profiles
        (staff_number,first_name,last_name,store_name,sex,phone,email,
         address_1,address_2,address_3,postcode,date_joined,date_of_birth,
         contracted_hrs,hourly_rate,is_salaried,salary_amount,is_active)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (form.get("staff_number") or None, fv("first_name"), fv("last_name"),
       fv("store_name"), fv("sex"), fv("phone"), fv("email"),
       fv("address_1"), fv("address_2"), fv("address_3"), fv("postcode"),
       fv("date_joined") or None, fv("date_of_birth") or None,
       fn("contracted_hrs"), fn("hourly_rate"),
       fv("is_salaried","N"), fn("salary_amount"),
       int(form.get("is_active", 1))))
    from urllib.parse import quote as uq
    return RedirectResponse(f"/staff?msg={uq('Staff member added successfully')}", status_code=303)


@router.post("/staff/{staff_id}/save")
async def save_staff(staff_id: int, request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    form = await request.form()
    fv = lambda k, d="": str(form.get(k, d) or d).strip()
    fn = lambda k: float(form.get(k, 0) or 0) if form.get(k) else None
    is_mgr = user["role"] in ("owner","manager")

    if is_mgr:
        q("""UPDATE staff_profiles SET
            staff_number=?,first_name=?,last_name=?,store_name=?,sex=?,
            phone=?,email=?,address_1=?,address_2=?,address_3=?,postcode=?,
            date_joined=?,date_of_birth=?,contracted_hrs=?,hourly_rate=?,
            is_salaried=?,salary_amount=?,is_active=?,date_left=?,leaving_reason=?
            WHERE staff_id=?""",
          (form.get("staff_number") or None, fv("first_name"), fv("last_name"),
           fv("store_name"), fv("sex"), fv("phone"), fv("email"),
           fv("address_1"), fv("address_2"), fv("address_3"), fv("postcode"),
           fv("date_joined") or None, fv("date_of_birth") or None,
           fn("contracted_hrs"), fn("hourly_rate"),
           fv("is_salaried","N"), fn("salary_amount"),
           int(form.get("is_active",1)),
           fv("date_left") or None, fv("leaving_reason") or None,
           staff_id))
    else:
        # Staff can only update personal contact details
        q("""UPDATE staff_profiles SET
            phone=?,email=?,address_1=?,address_2=?,address_3=?,postcode=?,date_of_birth=?
            WHERE staff_id=?""",
          (fv("phone"),fv("email"),fv("address_1"),fv("address_2"),
           fv("address_3"),fv("postcode"),fv("date_of_birth") or None, staff_id))

    from urllib.parse import quote as uq
    return RedirectResponse(f"/staff/{staff_id}?msg={uq('Profile updated')}", status_code=303)


@router.get("/staff/{staff_id}/request-leave", response_class=HTMLResponse)
def request_leave_form(staff_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"
    leave = get_leave_summary(staff_id)
    bal   = leave.get("balance_days", 0)

    type_opts = "".join(f"<option value='{k}'>{v}</option>" for k,v in ABSENCE_TYPES.items())

    content = f"""
    <div>
      <a href='/staff/{staff_id}' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to {name}</a>
      <div class='text-2xl font-black text-slate-800 mt-1'>📅 Request Leave — {name}</div>
    </div>
    <div class='card' style='max-width:520px'>
      <div style='background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:13px'>
        Current balance: <strong style='color:#0369a1'>{leave.get('balance_fmt','—')} remaining</strong>
      </div>
      <form action='/staff/{staff_id}/request-leave' method='POST' class='space-y-4'>
        <div><label>Leave Type</label>
          <select name='leave_type'>{type_opts}</select></div>
        <div class='grid gap-3' style='grid-template-columns:1fr 1fr'>
          <div><label>From Date</label>
            <input type='date' name='date_from' required></div>
          <div><label>To Date</label>
            <input type='date' name='date_to' required></div>
        </div>
        <div><label>Notes (optional)</label>
          <textarea name='notes' rows='2' placeholder='Any additional details...'></textarea></div>
        <div style='display:flex;gap:8px'>
          <button type='submit' class='btn-primary'>📤 Submit Request</button>
          <a href='/staff/{staff_id}' class='btn-secondary'>Cancel</a>
        </div>
      </form>
    </div>
    <script>
    // Auto-calculate working days when dates change
    document.addEventListener('DOMContentLoaded', function() {{
      const from = document.querySelector('[name="date_from"]');
      const to   = document.querySelector('[name="date_to"]');
      if (from) from.addEventListener('change', function() {{
        if (to && !to.value) to.value = from.value;
      }});
    }});
    </script>"""
    return page("Request Leave", content, user, "staff")


@router.post("/staff/{staff_id}/request-leave")
async def submit_leave(staff_id: int, request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    form       = await request.form()
    leave_type = form.get("leave_type","H")
    date_from  = form.get("date_from","")
    date_to    = form.get("date_to","")
    notes      = str(form.get("notes","") or "").strip()

    # Calculate working days
    try:
        d1 = datetime.strptime(date_from, "%Y-%m-%d")
        d2 = datetime.strptime(date_to,   "%Y-%m-%d")
        days = 0
        cur  = d1
        while cur <= d2:
            if cur.weekday() < 5 and cur.strftime("%Y-%m-%d") not in UK_BANK_HOLIDAYS_2026:
                days += 1
            cur = cur + timedelta(days=1)
    except Exception:
        days = 1

    # Auto-approve for manager/owner, else pending
    status = "approved" if user["role"] in ("owner","manager") else "pending"

    q("""INSERT INTO leave_requests
        (staff_id, leave_type, date_from, date_to, days_taken,
         status, requested_by, notes)
        VALUES(?,?,?,?,?,?,?,?)""",
      (staff_id, leave_type, date_from, date_to, days,
       status, user.get("username"), notes or None))

    from urllib.parse import quote as uq
    msg = "Leave approved ✅" if status=="approved" else "Leave request submitted — awaiting approval ⏳"
    return RedirectResponse(f"/staff/{staff_id}?msg={uq(msg)}", status_code=303)


@router.post("/staff/leave-requests/{req_id}/approve")
def approve_leave(req_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    q("UPDATE leave_requests SET status='approved', approved_by=?, approved_at=datetime('now') WHERE request_id=?",
      (user.get("username"), req_id))
    return RedirectResponse("/staff/leave-requests", status_code=303)


@router.post("/staff/leave-requests/{req_id}/decline")
def decline_leave(req_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    q("UPDATE leave_requests SET status='declined', approved_by=?, approved_at=datetime('now') WHERE request_id=?",
      (user.get("username"), req_id))
    return RedirectResponse("/staff/leave-requests", status_code=303)


@router.get("/staff/{staff_id}/pay-history", response_class=HTMLResponse)
def pay_history_page(staff_id: int, session: str | None = Cookie(default=None), msg: str = ""):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner", "manager"):
        return RedirectResponse("/staff", status_code=303)

    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"

    # Current NMW
    today   = datetime.now().strftime("%Y-%m-%d")
    nmw     = get_nmw_for_person(s.get("date_of_birth",""), today)
    current = s.get("hourly_rate") or 0
    diff    = round(current - nmw, 2)
    dob_str = s.get("date_of_birth","")
    age     = ((datetime.now() - datetime.strptime(dob_str,"%Y-%m-%d")).days // 365) if dob_str else 0

    # NMW status badge
    if nmw == 0:
        nmw_badge = "<span class='badge-unpaid'>DOB not set</span>"
    elif diff < 0:
        nmw_badge = f"<span class='badge-overdue'>⚠️ £{abs(diff):.2f} BELOW NMW</span>"
    elif diff < 0.50:
        nmw_badge = f"<span class='badge-partial'>⚠️ Only £{diff:.2f} above NMW</span>"
    else:
        nmw_badge = f"<span class='badge-paid'>✅ £{diff:.2f} above NMW</span>"

    # Pay history
    history = q("""SELECT * FROM pay_history WHERE staff_id=?
                   ORDER BY effective_date DESC""", (staff_id,), fetch=True) or []

    hist_rows = ""
    for h in history:
        h = dict(h)
        prev = f"£{h['previous_rate']:.2f}" if h.get("previous_rate") else "—"
        change = ""
        if h.get("previous_rate") and h.get("hourly_rate"):
            pct = ((h["hourly_rate"] - h["previous_rate"]) / h["previous_rate"]) * 100
            change = f"<span style='color:{'#16a34a' if pct>=0 else '#dc2626'};font-weight:700'>{'▲' if pct>=0 else '▼'} {abs(pct):.1f}%</span>"
        hist_rows += f"""<tr>
          <td class='mono'>{h['effective_date']}</td>
          <td class='mono' style='font-weight:700'>£{h['hourly_rate']:.2f}/hr</td>
          <td class='mono'>{prev}</td>
          <td>{change}</td>
          <td style='font-size:12px;color:#64748b'>{h.get('change_reason') or '—'}</td>
          <td style='font-size:12px;color:#94a3b8'>{h.get('recorded_by') or '—'}</td>
        </tr>"""

    # NMW history table
    nmw_rows = ""
    all_nmw = q("SELECT * FROM nmw_rates ORDER BY effective_date DESC", fetch=True) or []
    for n in all_nmw:
        n   = dict(n)
        age_rate = n["rate_21_plus"] if age >= 21 else (n["rate_18_20"] if age >= 18 else n["rate_16_17"])
        nmw_rows += f"""<tr>
          <td class='mono'>{n['effective_date']}</td>
          <td class='mono'>£{n['rate_21_plus']:.2f}</td>
          <td class='mono'>£{n['rate_18_20']:.2f}</td>
          <td class='mono'>£{n['rate_16_17']:.2f}</td>
          <td class='mono' style='font-weight:700;color:#0369a1'>£{age_rate:.2f}</td>
        </tr>"""

    flash = f"<div class='flash-success'>{msg}</div>" if msg else ""

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <a href='/staff/{staff_id}' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to {name}</a>
        <div class='text-2xl font-black text-slate-800 mt-1'>💰 Pay History — {name}</div>
      </div>
    </div>

    <!-- Current pay status -->
    <div class='grid gap-4' style='grid-template-columns:repeat(auto-fit,minmax(160px,1fr))'>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Current Rate</div>
        <div style='font-size:28px;font-weight:900;color:#0f2942'>£{current:.2f}</div>
        <div style='font-size:11px;color:#94a3b8'>per hour</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>NMW (Age {age})</div>
        <div style='font-size:28px;font-weight:900;color:#0f2942'>£{nmw:.2f}</div>
        <div style='font-size:11px;color:#94a3b8'>minimum wage</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Status</div>
        <div style='margin-top:8px'>{nmw_badge}</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Annual Equiv.</div>
        <div style='font-size:22px;font-weight:900;color:#0f2942'>£{(current * (s.get('contracted_hrs') or 0) * 52):,.0f}</div>
        <div style='font-size:11px;color:#94a3b8'>based on {s.get('contracted_hrs') or 0}h/wk</div>
      </div>
    </div>

    <!-- Record new pay change -->
    <div class='card' style='max-width:500px'>
      <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>➕ Record Pay Change</div>
      <form action='/staff/{staff_id}/pay-history' method='POST' class='grid gap-3' style='grid-template-columns:1fr 1fr'>
        <div><label>Effective Date</label>
          <input type='date' name='effective_date' value='{today}' required></div>
        <div><label>New Hourly Rate (£)</label>
          <input type='number' step='0.01' name='hourly_rate' placeholder='e.g. 12.71' required></div>
        <div style='grid-column:1/-1'><label>Reason</label>
          <input type='text' name='change_reason' placeholder='e.g. Annual review, NMW increase, Promotion'></div>
        <div style='grid-column:1/-1'>
          <button type='submit' class='btn-primary'>💾 Save Pay Change</button>
        </div>
      </form>
    </div>

    <!-- Pay history -->
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:12px 16px;background:#0f2942;color:white;font-weight:700;font-size:14px'>Pay History</div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>Effective Date</th><th>Rate</th><th>Previous</th><th>Change</th><th>Reason</th><th>Recorded By</th></tr></thead>
          <tbody>{hist_rows or '<tr><td colspan="6" style="text-align:center;padding:24px;color:#94a3b8">No pay history recorded yet</td></tr>'}</tbody>
        </table>
      </div>
    </div>

    <!-- NMW reference table -->
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:12px 16px;background:#0f2942;color:white;font-weight:700;font-size:14px'>
        National Minimum Wage Reference
        <span style='font-size:12px;font-weight:400;color:#93c5fd;margin-left:8px'>Highlighted column = this employee's applicable rate</span>
      </div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead><tr><th>Effective</th><th>21+</th><th>18-20</th><th>16-17</th><th style='background:#1e3a5f'>Applicable Rate</th></tr></thead>
          <tbody>{nmw_rows}</tbody>
        </table>
      </div>
    </div>"""
    return page(f"Pay History — {name}", content, user, "staff")


@router.post("/staff/{staff_id}/pay-history")
async def save_pay_change(staff_id: int, request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse(f"/staff/{staff_id}", status_code=303)
    form = await request.form()
    new_rate  = float(form.get("hourly_rate", 0) or 0)
    eff_date  = form.get("effective_date","")
    reason    = str(form.get("change_reason","") or "").strip()

    # Get previous rate
    current = q("SELECT hourly_rate FROM staff_profiles WHERE staff_id=?",
                (staff_id,), fetch=True)
    prev_rate = dict(current[0])["hourly_rate"] if current else None

    q("""INSERT INTO pay_history (staff_id,effective_date,hourly_rate,previous_rate,change_reason,recorded_by)
         VALUES(?,?,?,?,?,?)""",
      (staff_id, eff_date, new_rate, prev_rate, reason or None, user.get("username")))

    # Update current rate on profile
    q("UPDATE staff_profiles SET hourly_rate=? WHERE staff_id=?", (new_rate, staff_id))

    from urllib.parse import quote as uq
    return RedirectResponse(f"/staff/{staff_id}/pay-history?msg={uq('Pay change recorded')}", status_code=303)


@router.get("/staff/{staff_id}/set-entitlement", response_class=HTMLResponse)
def set_entitlement_form(staff_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse(f"/staff/{staff_id}", status_code=303)
    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"
    year = datetime.now().year
    contracted = s.get("contracted_hrs") or 0
    # Statutory minimum is always 5.6 weeks x 5 days = 28 days
    # (regardless of hours — part-timers get pro-rated days but
    #  the day count is the same; the difference is day length)
    statutory  = 28.0
    # Pro-rata = 5.6 weeks × contracted hours ÷ 5 (actual days entitlement)
    pro_rata   = round(5.6 * contracted / 5, 1) if contracted else 28.0
    existing   = q("SELECT * FROM leave_entitlements WHERE staff_id=? AND year=?",
                   (staff_id, year), fetch=True)
    current    = dict(existing[0]) if existing else {}
    cur_val    = current.get("custom_days") or pro_rata

    html  = f"""
    <div>
      <a href='/staff/{staff_id}' style='color:#1e3a5f;font-size:13px;font-weight:700'>
        &larr; Back to {name}</a>
      <div class='text-2xl font-black text-slate-800 mt-1'>
        &#9881;&#65039; Leave Entitlement &mdash; {name} ({year})</div>
    </div>
    <div class='card' style='max-width:520px'>
      <div style='background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;
                  padding:12px 16px;margin-bottom:16px;font-size:13px'>
        <div><strong>Contracted hours:</strong> {contracted}h/week</div>
        <div><strong>Statutory minimum:</strong> 28 days (5.6 weeks)</div>
        <div style='font-size:12px;color:#64748b;margin-top:4px'>
          For part-time staff you can set below 28 days — pro-rata
          is acceptable (e.g. 3 days/wk = 5.6 × 3 = 16.8 days minimum).
          Set whatever you have agreed contractually.
        </div>
      </div>
      <form action='/staff/{staff_id}/set-entitlement' method='POST' class='space-y-4'>
        <input type='hidden' name='year' value='{year}'>
        <div>
          <label>Custom Entitlement (days) for {year}</label>
          <div style='display:flex;gap:8px;align-items:center'>
            <input type='number' step='0.5' name='custom_days' id='custom_days_input'
                   value='{cur_val}' min='1' style='flex:1'>
            <button type='button'
              onclick="document.getElementById('custom_days_input').value='{pro_rata}';
                       document.getElementById('notes_input').value='Reset to statutory pro-rata';"
              class='btn-secondary' style='white-space:nowrap;padding:8px 14px;font-size:12px'>
              🔄 Reset to Statutory ({pro_rata} days)
            </button>
          </div>
          <div style='font-size:11px;color:#94a3b8;margin-top:4px'>
            Statutory pro-rata for {contracted}h/wk =
            5.6 weeks × {contracted}h ÷ 5 days/wk = {pro_rata} days
          </div>
        </div>
        <div>
          <label>Notes</label>
          <input type='text' name='notes' id='notes_input'
                 value='{current.get("notes") or ""}'
                 placeholder='e.g. Pro-rated, contractual agreement'>
        </div>
        <div style='display:flex;gap:8px'>
          <button type='submit' class='btn-primary'>💾 Save Entitlement</button>
          <a href='/staff/{staff_id}' class='btn-secondary'>Cancel</a>
        </div>
      </form>
    </div>"""
    return page(f"Entitlement", html, user, "staff")


@router.post("/staff/{staff_id}/set-entitlement")
async def save_entitlement(staff_id: int, request: Request,
                           session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    form        = await request.form()
    year        = int(form.get("year", datetime.now().year))
    custom_days = float(form.get("custom_days", 0) or 0)
    notes       = str(form.get("notes","") or "").strip()
    s           = q("SELECT contracted_hrs FROM staff_profiles WHERE staff_id=?",
                    (staff_id,), fetch=True)
    contracted  = dict(s[0])["contracted_hrs"] if s else 0
    statutory   = 28.0  # Always 28 days statutory minimum
    q("""INSERT INTO leave_entitlements
            (staff_id,year,statutory_days,custom_days,effective_days,notes)
         VALUES(?,?,?,?,?,?)
         ON CONFLICT(staff_id,year) DO UPDATE SET
            custom_days=excluded.custom_days,
            effective_days=excluded.effective_days,
            notes=excluded.notes""",
      (staff_id, year, statutory, custom_days, custom_days, notes or None))
    from urllib.parse import quote as uq
    return RedirectResponse(f"/staff/{staff_id}?msg={uq('Entitlement updated')}",
                            status_code=303)


@router.get("/staff/{staff_id}/documents", response_class=HTMLResponse)
def staff_documents(
    staff_id: int,
    session:  str | None = Cookie(default=None),
    msg:      str = "",
    msg_type: str = "success"
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r

    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"

    # Get all documents for this staff member
    docs = q("""SELECT * FROM staff_documents WHERE staff_id=?
                ORDER BY doc_type, version DESC""",
             (staff_id,), fetch=True) or []

    # Get available templates
    templates = q("SELECT * FROM document_templates WHERE is_current=1 ORDER BY doc_type",
                  fetch=True) or []
    template_types = {dict(t)["doc_type"] for t in templates}

    flash = f"<div class='flash-{'success' if msg_type=='success' else 'error'}'>{msg}</div>" if msg else ""

    # Group docs by type
    from collections import defaultdict
    by_type = defaultdict(list)
    for d in docs:
        by_type[dict(d)["doc_type"]].append(dict(d))

    # Build document cards
    doc_cards = ""
    for dtype in DOC_TYPES:
        type_docs = by_type.get(dtype, [])
        has_template = dtype in template_types

        # Current version
        current = next((d for d in type_docs if d["is_current"]), None)
        older   = [d for d in type_docs if not d["is_current"]]

        current_html = ""
        if current:
            gen_badge = "<span style='background:#dbeafe;color:#1d4ed8;font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px'>AUTO-GENERATED</span>" if current["generated"] else ""
            current_html = f"""
            <div style='background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px'>
              <div>
                <div style='font-size:13px;font-weight:700;color:#166534'>
                  ✅ v{current['version']} — {current['uploaded_at'][:10]} {gen_badge}
                </div>
                <div style='font-size:11px;color:#64748b'>{current.get('notes') or ''}</div>
              </div>
              <div style='display:flex;gap:6px'>
                <a href='/staff/{staff_id}/documents/{current["doc_id"]}/download'
                   class='btn-secondary' style='padding:4px 10px;font-size:11px'>⬇️ Download</a>
                <a href='/staff/{staff_id}/documents/{current["doc_id"]}/view'
                   class='btn-secondary' style='padding:4px 10px;font-size:11px' target='_blank'>👁 View</a>
              </div>
            </div>"""

        older_html = ""
        if older:
            older_html = "<div style='margin-top:6px'>"
            for od in older:
                older_html += f"""
                <div style='display:flex;justify-content:space-between;align-items:center;padding:6px 10px;font-size:12px;color:#64748b;border-bottom:1px solid #f1f5f9'>
                  <span>v{od['version']} — {od['uploaded_at'][:10]}</span>
                  <a href='/staff/{staff_id}/documents/{od["doc_id"]}/download'
                     style='color:#1e3a5f;font-weight:700;font-size:11px'>⬇️ Download</a>
                </div>"""
            older_html += "</div>"

        # Upload / generate form
        action_html = f"""
        <div style='margin-top:10px;padding-top:10px;border-top:1px solid #f1f5f9'>
          <div style='display:flex;gap:8px;flex-wrap:wrap'>
            <form action='/staff/{staff_id}/documents/upload' method='POST'
                  enctype='multipart/form-data' style='display:flex;gap:6px;flex:1;min-width:200px'>
              <input type='hidden' name='doc_type' value='{dtype}'>
              <input type='file' name='doc_file' accept='.pdf,.doc,.docx,.dotx'
                     style='flex:1;font-size:12px;padding:4px 8px'>
              <button type='submit' class='btn-primary' style='padding:4px 12px;font-size:11px;white-space:nowrap'>
                ⬆️ Upload
              </button>
            </form>
            {"<a href='/staff/" + str(staff_id) + "/documents/generate?doc_type=" + dtype + "' class='btn-secondary' style='padding:4px 12px;font-size:11px;white-space:nowrap'>⚡ Auto-fill</a>" if has_template else ""}
          </div>
        </div>"""

        doc_cards += f"""
        <div class='card'>
          <div style='font-weight:900;color:#0f2942;margin-bottom:8px;font-size:14px'>{dtype}</div>
          {current_html or "<div style='color:#94a3b8;font-size:13px'>No document uploaded yet</div>"}
          {older_html}
          {action_html}
        </div>"""

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <a href='/staff/{staff_id}' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to {name}</a>
        <div class='text-2xl font-black text-slate-800 mt-1'>📁 Documents — {name}</div>
      </div>
      {'<a href="/staff/document-templates" class="btn-secondary">📋 Manage Templates</a>' if user["role"] == "owner" else ''}
    </div>
    <div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(400px,1fr))'>
      {doc_cards}
    </div>"""

    return page(f"Documents — {name}", content, user, "staff")


@router.post("/staff/{staff_id}/documents/upload")
async def upload_staff_doc(
    staff_id: int,
    request:  Request,
    session:  str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r

    form     = await request.form()
    doc_type = form.get("doc_type","Other")
    doc_file = form.get("doc_file")
    notes    = str(form.get("notes","") or "").strip()

    if not doc_file or not hasattr(doc_file, "filename") or not doc_file.filename:
        from urllib.parse import quote as uq
        return RedirectResponse(
            f"/staff/{staff_id}/documents?msg={uq('No file selected')}&msg_type=error",
            status_code=303)

    # Get next version number
    existing = q("""SELECT MAX(version) as v FROM staff_documents
                    WHERE staff_id=? AND doc_type=?""",
                 (staff_id, doc_type), fetch=True)
    next_ver = (existing[0]["v"] or 0) + 1 if existing else 1

    # Mark previous versions as not current
    q("UPDATE staff_documents SET is_current=0 WHERE staff_id=? AND doc_type=?",
      (staff_id, doc_type))

    # Save file (sanitise name parts to prevent path traversal; whitelist ext; cap size)
    ext      = os.path.splitext(doc_file.filename)[1].lower()
    filename = f"staff_{staff_id}_{_safe_part(doc_type)}_v{next_ver}{_safe_ext(ext)}"
    filepath = os.path.join(DOCS_DIR, filename)
    data = await doc_file.read()
    if len(data) > 25 * 1024 * 1024:
        from urllib.parse import quote as uq
        return RedirectResponse(f"/staff/{staff_id}/documents?msg={uq('File too large (max 25 MB)')}&msg_type=error",
                                status_code=303)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(data)

    q("""INSERT INTO staff_documents
            (staff_id, doc_type, version, file_path, file_name,
             is_current, generated, uploaded_by, notes)
         VALUES(?,?,?,?,?,1,0,?,?)""",
      (staff_id, doc_type, next_ver, filepath,
       doc_file.filename, user.get("username"), notes or None))

    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/staff/{staff_id}/documents?msg={uq(doc_type + ' uploaded successfully')}",
        status_code=303)


@router.get("/staff/{staff_id}/documents/generate", response_class=HTMLResponse)
def generate_doc_form(
    staff_id: int,
    doc_type: str = "",
    session:  str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse(f"/staff/{staff_id}/documents", status_code=303)

    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"

    # Get available templates
    templates = q("SELECT * FROM document_templates WHERE is_current=1 ORDER BY doc_type",
                  fetch=True) or []

    # Show merge fields preview
    fields     = get_merge_fields(s)
    fields_html = ""
    for k, v in fields.items():
        fields_html += f"""
        <div style='display:flex;gap:12px;padding:4px 0;border-bottom:1px solid #f1f5f9;font-size:12px'>
          <code style='color:#7c3aed;min-width:180px'>{k}</code>
          <span style='color:#334155'>{v or '—'}</span>
        </div>"""

    type_opts = ""
    for t in templates:
        td = dict(t)
        sel = "selected" if td["doc_type"] == doc_type else ""
        type_opts += f'<option value="{td["doc_type"]}" {sel}>{td["doc_type"]} (v{td["version"]})</option>'

    content = f"""
    <div>
      <a href='/staff/{staff_id}/documents' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to Documents</a>
      <div class='text-2xl font-black text-slate-800 mt-1'>⚡ Auto-Generate Document — {name}</div>
    </div>
    <div class='grid gap-6' style='grid-template-columns:1fr 1fr'>
      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Generate Document</div>
        <form action='/staff/{staff_id}/documents/generate' method='POST' class='space-y-4'>
          <div>
            <label>Document Type</label>
            <select name='doc_type' required>
              <option value=''>-- Select template --</option>
              {type_opts}
            </select>
          </div>
          <div>
            <label>Notes (optional)</label>
            <input type='text' name='notes' placeholder='e.g. Initial offer, Updated contract'>
          </div>
          <button type='submit' class='btn-primary'>⚡ Generate & Download</button>
        </form>
        {'<div class="flash-error" style="margin-top:12px">No templates uploaded yet. <a href=\'  /staff/document-templates\' style=\'color:#1e3a5f;font-weight:700\'>Upload templates here →</a></div>' if not templates else ''}
      </div>
      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Available Merge Fields</div>
        <div style='font-size:11px;color:#64748b;margin-bottom:8px'>
          Use these placeholders in your Word template — they will be replaced with this staff member's details.
        </div>
        <div style='max-height:400px;overflow-y:auto'>
          {fields_html}
        </div>
      </div>
    </div>"""

    return page("Generate Document", content, user, "staff")


@router.post("/staff/{staff_id}/documents/generate")
async def generate_doc(
    staff_id: int,
    request:  Request,
    session:  str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r

    form     = await request.form()
    doc_type = form.get("doc_type","")
    notes    = str(form.get("notes","") or "").strip()

    # Get staff details
    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s = dict(rows[0])

    # Get template
    tmpl = q("SELECT * FROM document_templates WHERE doc_type=? AND is_current=1",
             (doc_type,), fetch=True)
    if not tmpl:
        from urllib.parse import quote as uq
        return RedirectResponse(
            f"/staff/{staff_id}/documents?msg={uq('No template found for ' + doc_type)}&msg_type=error",
            status_code=303)
    tmpl = dict(tmpl[0])

    # Fill template
    fields   = get_merge_fields(s)
    doc_bytes = fill_word_template(tmpl["file_path"], fields)

    # Save generated file
    existing = q("""SELECT MAX(version) as v FROM staff_documents
                    WHERE staff_id=? AND doc_type=?""",
                 (staff_id, doc_type), fetch=True)
    next_ver = (existing[0]["v"] or 0) + 1 if existing else 1
    q("UPDATE staff_documents SET is_current=0 WHERE staff_id=? AND doc_type=?",
      (staff_id, doc_type))

    filename = f"staff_{staff_id}_{_safe_part(doc_type)}_v{next_ver}.docx"
    filepath = os.path.join(DOCS_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(doc_bytes)

    q("""INSERT INTO staff_documents
            (staff_id, doc_type, version, file_path, file_name,
             is_current, generated, uploaded_by, notes)
         VALUES(?,?,?,?,?,1,1,?,?)""",
      (staff_id, doc_type, next_ver, filepath, filename,
       user.get("username"), notes or None))

    # Return the file for download
    name = f"{s['first_name']} {s['last_name']}"
    download_name = f"{doc_type} - {name}.docx"
    return FileResponse(filepath, filename=download_name,
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.get("/staff/{staff_id}/documents/{doc_id}/download")
def download_doc(staff_id: int, doc_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    rows = q("SELECT * FROM staff_documents WHERE doc_id=? AND staff_id=?",
             (doc_id, staff_id), fetch=True)
    if not rows: return HTMLResponse("<p>Document not found</p>", status_code=404)
    d = dict(rows[0])
    if not os.path.exists(d["file_path"]):
        return HTMLResponse("<p>File not found on disk</p>", status_code=404)
    ext = os.path.splitext(d["file_path"])[1].lower()
    media = "application/pdf" if ext == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return FileResponse(d["file_path"], filename=d["file_name"] or os.path.basename(d["file_path"]),
                        media_type=media)


@router.get("/staff/{staff_id}/documents/{doc_id}/view")
def view_doc(staff_id: int, doc_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    rows = q("SELECT * FROM staff_documents WHERE doc_id=? AND staff_id=?",
             (doc_id, staff_id), fetch=True)
    if not rows: return HTMLResponse("<p>Document not found</p>", status_code=404)
    d = dict(rows[0])
    if not os.path.exists(d["file_path"]):
        return HTMLResponse("<p>File not found on disk</p>", status_code=404)
    return FileResponse(d["file_path"], media_type="application/pdf")


def ensure_onboarding_tables():
    conn = db()
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS onboarding_forms (
            form_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id       INTEGER NOT NULL,
            form_type      TEXT NOT NULL,
            status         TEXT DEFAULT 'not_started',
            started_at     TEXT,
            completed_at   TEXT,
            form_data      TEXT,
            pdf_path       TEXT,
            UNIQUE(staff_id, form_type),
            FOREIGN KEY (staff_id) REFERENCES staff_profiles(staff_id)
        )
    """)
    conn.commit()
    conn.close()


ONBOARD_FORMS = [
    ("employment_application", "Employment Application",  "staff"),
    ("p46",                    "P46 Tax Form",            "staff"),
    ("new_employee_notify",    "New Employee Notification","owner"),
    ("offer_letter",           "Offer Letter",            "owner"),
    ("employment_contract",    "Employment Contract",     "owner"),
    ("right_to_work",          "Right to Work Checked",  "owner"),
]


DIGITAL_FORMS = {"employment_application", "p46", "new_employee_notify"}


def get_onboarding_status(staff_id: int) -> dict:
    """Return completion status for each onboarding form and document."""
    rows = q("SELECT form_type, status FROM onboarding_forms WHERE staff_id=?",
             (staff_id,), fetch=True) or []
    status_map = {dict(r)["form_type"]: dict(r)["status"] for r in rows}

    # Check staff_documents for document-based items
    doc_rows = q("SELECT doc_type, is_current FROM staff_documents WHERE staff_id=? AND is_current=1",
                 (staff_id,), fetch=True) or []
    doc_types = {dict(d)["doc_type"] for d in doc_rows}

    # Map document types to onboarding form types
    doc_type_map = {
        "offer_letter":        "Offer Letter",
        "employment_contract": "Employment Contract",
        "right_to_work":       "Right to Work",
    }

    result = {}
    for ftype, flabel, fwho in ONBOARD_FORMS:
        # Check if it's a document-based item
        if ftype in doc_type_map:
            doc_label = doc_type_map[ftype]
            status = "completed" if doc_label in doc_types else status_map.get(ftype, "not_started")
        else:
            status = status_map.get(ftype, "not_started")
        result[ftype] = {
            "label":   flabel,
            "who":     fwho,
            "status":  status,
            "is_doc":  ftype not in DIGITAL_FORMS,
        }
    return result


def onboard_status_badge(status: str) -> str:
    return {
        "not_started": "<span style='background:#f1f5f9;color:#64748b;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px'>Not Started</span>",
        "in_progress": "<span style='background:#fef3c7;color:#d97706;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px'>In Progress</span>",
        "completed":   "<span style='background:#dcfce7;color:#16a34a;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px'>✅ Complete</span>",
    }.get(status, status)


@router.get("/staff/{staff_id}/onboarding", response_class=HTMLResponse)
def onboarding_overview(
    staff_id: int,
    session:  str | None = Cookie(default=None),
    msg:      str = ""
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r

    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"

    ob_status = get_onboarding_status(staff_id)
    is_owner  = user["role"] == "owner"
    flash     = f"<div class='flash-success'>{msg}</div>" if msg else ""

    # Build checklist
    checklist = ""
    all_done  = all(v["status"] == "completed" for v in ob_status.values())

    for ftype, info in ob_status.items():
        # Staff only see their own forms
        if info["who"] == "owner" and not is_owner:
            continue

        status  = info["status"]
        badge   = onboard_status_badge(status)
        is_doc  = info.get("is_doc", False)

        if is_doc:
            # Document-based — link to documents page
            btn_url = f"/staff/{staff_id}/documents"
            btn_lbl = "Go to Documents →" if status != "completed" else "View Documents →"
            btn_cls = "btn-primary" if status != "completed" else "btn-secondary"
        else:
            btn_url = f"/staff/{staff_id}/onboarding/{ftype}"
            btn_lbl = "Start →" if status == "not_started" else ("Continue →" if status == "in_progress" else "View →")
            btn_cls = "btn-primary" if status != "completed" else "btn-secondary"

        # Upload signed copy option (for forms only, not doc-based items)
        upload_html = ""
        if not is_doc and status != "completed":
            upload_html = f"""
        <form action='/staff/{staff_id}/onboarding/{ftype}/upload-paper' method='POST'
              enctype='multipart/form-data'
              style='display:inline-flex;gap:6px;align-items:center;margin-left:8px;margin-top:6px'>
          <input type='file' name='paper_form' accept='.pdf,.jpg,.jpeg,.png'
                 style='font-size:11px;max-width:160px;padding:3px'>
          <button type='submit' class='btn-secondary' style='padding:3px 8px;font-size:11px;white-space:nowrap'>
            &#128196; Upload Signed Copy (PDF/Scan)
          </button>
        </form>"""

        # PDF download if completed
        pdf_link = ""
        if status == "completed":
            pdf_row = q("SELECT pdf_path FROM onboarding_forms WHERE staff_id=? AND form_type=?",
                        (staff_id, ftype), fetch=True)
            if pdf_row and dict(pdf_row[0]).get("pdf_path"):
                pdf_link = f"<a href='/staff/{staff_id}/onboarding/{ftype}/pdf' target='_blank' style='color:#1e3a5f;font-size:12px;font-weight:700;margin-left:8px'>📄 PDF</a>"

        owner_tag = "<span style='font-size:10px;color:#94a3b8;margin-left:6px'>(owner only)</span>" if info["who"] == "owner" else ""

        checklist += f"""
        <div style='padding:14px 16px;border-bottom:1px solid #f1f5f9'>
          <div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px'>
            <div>
              <div style='font-weight:700;color:#0f172a;font-size:14px'>{info['label']}{owner_tag}</div>
              <div style='margin-top:4px'>{badge}{pdf_link}</div>
            </div>
            <a href='{btn_url}' class='{btn_cls}' style='padding:6px 16px;font-size:13px'>{btn_lbl}</a>
          </div>
          {upload_html}
        </div>"""

    completion_bar = ""
    completed_n = sum(1 for v in ob_status.values() if v["status"] == "completed")
    total_n     = len(ob_status)
    pct         = int(completed_n / total_n * 100)
    bar_col     = "#16a34a" if pct == 100 else ("#d97706" if pct > 0 else "#e2e8f0")
    completion_bar = f"""
    <div class='card'>
      <div style='display:flex;justify-content:space-between;margin-bottom:6px'>
        <span style='font-size:13px;font-weight:700;color:#0f172a'>Onboarding Progress</span>
        <span style='font-size:13px;font-weight:700;color:{bar_col}'>{completed_n}/{total_n} complete</span>
      </div>
      <div style='background:#f1f5f9;border-radius:99px;height:8px'>
        <div style='background:{bar_col};border-radius:99px;height:8px;width:{pct}%;transition:width .3s'></div>
      </div>
      {'<div style="font-size:12px;color:#16a34a;font-weight:700;margin-top:6px">🎉 All onboarding forms complete!</div>' if pct==100 else ''}
    </div>"""

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <a href='/staff/{staff_id}' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to {name}</a>
        <div class='text-2xl font-black text-slate-800 mt-1'>📋 Onboarding — {name}</div>
      </div>
    </div>
    {completion_bar}
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:12px 16px;background:#0f2942;color:white;font-weight:700;font-size:14px'>
        Onboarding Checklist
      </div>
      {checklist}
    </div>"""

    return page("Onboarding", content, user, "staff")


@router.get("/staff/{staff_id}/onboarding/employment_application", response_class=HTMLResponse)
def employment_application_form(staff_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r

    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"

    # Get any saved data
    saved = q("SELECT form_data FROM onboarding_forms WHERE staff_id=? AND form_type='employment_application'",
              (staff_id,), fetch=True)
    import json
    data = json.loads(dict(saved[0])["form_data"]) if saved and dict(saved[0])["form_data"] else {}

    # Only pre-fill from profile if form has been started before
    has_data = bool(data)
    def fv(k, default=""): return data.get(k, default) if has_data else ""
    def fi(name, label, ftype="text", val=None, req=False, placeholder=""):
        v    = val if val is not None else fv(name)
        req_a = "required" if req else ""
        ph    = f"placeholder='{placeholder}'" if placeholder else ""
        return f"<div><label>{label}</label><input type='{ftype}' name='{name}' value='{v}' {req_a} {ph}></div>"

    content = f"""
    <div>
      <a href='/staff/{staff_id}/onboarding' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to Onboarding</a>
      <div class='text-2xl font-black text-slate-800 mt-1'>Employment Application — {name}</div>
      <div style='font-size:13px;color:#64748b;margin-top:2px'>Snappy Snaps — Equal Opportunity Employer</div>
    </div>

    <form action='/staff/{staff_id}/onboarding/employment_application' method='POST' class='space-y-6'>

      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Position & Personal Details</div>
        <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>
          {fi('position_applied', 'Position Applied For', req=True)}
          {fi('full_name', 'Full Name', val=fv('full_name') or f"{s.get('first_name','')} {s.get('last_name','')}", req=True)}
          {fi('address',         'Address',               val=fv('address') or ', '.join(filter(None,[s.get('address_1',''),s.get('address_2',''),s.get('address_3',''),s.get('postcode','')])))}
          <div><label>Telephone No.</label>
            <input type='text' name='phone' value='{fv("phone") or s.get("phone","")}'
              placeholder='01234 567890'>
          </div>
          <div><label>Mobile No. <span style="font-size:10px;color:#94a3b8;font-weight:400">(preferred format: 07700 123456)</span></label>
            <input type='text' name='mobile' value='{fv("mobile")}'
              placeholder='07700 123456'>
          </div>
          {fi('ni_number',       'National Insurance No.',placeholder='AB 12 34 56 C')}
          <div><label>Driving Licence</label>
            <select name='driving_licence'>
              <option value=''>-- Select --</option>
              <option {'selected' if fv('driving_licence')=='Full' else ''}>Full</option>
              <option {'selected' if fv('driving_licence')=='Provisional' else ''}>Provisional</option>
              <option {'selected' if fv('driving_licence')=='None' else ''}>None</option>
            </select></div>
          <div><label>Work Permit Required?</label>
            <select name='work_permit'>
              <option value='No' {'selected' if fv('work_permit','No')=='No' else ''}>No</option>
              <option value='Yes' {'selected' if fv('work_permit')=='Yes' else ''}>Yes</option>
            </select></div>
        </div>
      </div>

      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Health Information</div>
        <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>
          {fi('health_state', 'Current State of Health', placeholder='e.g. Good')}
          <div><label>Respiratory Problems?</label>
            <select name='respiratory'><option value='No'>No</option><option value='Yes' {'selected' if fv('respiratory')=='Yes' else ''}>Yes</option></select></div>
          <div><label>Skin Irritation?</label>
            <select name='skin_irritation'><option value='No'>No</option><option value='Yes' {'selected' if fv('skin_irritation')=='Yes' else ''}>Yes</option></select></div>
          <div style='grid-column:1/-1'>
            <label>Absence from work through illness in past 12 months</label>
            <textarea name='illness_absence' rows='2' placeholder='Please give details if any'>{fv('illness_absence')}</textarea>
          </div>
          <div><label>Do you smoke?</label>
            <select name='smoking'>
              <option value='Never' {'selected' if fv('smoking','Never')=='Never' else ''}>Never</option>
              <option value='Socially' {'selected' if fv('smoking')=='Socially' else ''}>Socially</option>
              <option value='Sometimes' {'selected' if fv('smoking')=='Sometimes' else ''}>Sometimes</option>
              <option value='Over 20/day' {'selected' if fv('smoking')=='Over 20/day' else ''}>Over 20/day</option>
            </select></div>
        </div>
      </div>

      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Educational History</div>
        <div class='grid gap-3' style='grid-template-columns:1fr'>
          <div><label>O Levels / GCSEs (subjects and grades)</label>
            <textarea name='gcse' rows='2'>{fv('gcse')}</textarea></div>
          <div><label>A Levels</label>
            <textarea name='a_levels' rows='2'>{fv('a_levels')}</textarea></div>
          <div><label>University / Degree</label>
            <input type='text' name='university' value='{fv('university')}'></div>
          <div><label>Other Qualifications or Skills</label>
            <textarea name='other_quals' rows='2'>{fv('other_quals')}</textarea></div>
        </div>
      </div>

      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>General Information</div>
        <div class='grid gap-3' style='grid-template-columns:1fr'>
          <div><label>What do you seek most from this position?</label>
            <textarea name='seeks' rows='2'>{fv('seeks')}</textarea></div>
          <div><label>Where do you see yourself in 5 years?</label>
            <textarea name='five_years' rows='2'>{fv('five_years')}</textarea></div>
          <div><label>Interests and hobbies</label>
            <textarea name='hobbies' rows='2'>{fv('hobbies')}</textarea></div>
          <div><label>Greatest strengths</label>
            <textarea name='strengths' rows='2'>{fv('strengths')}</textarea></div>
          <div><label>Greatest weaknesses</label>
            <textarea name='weaknesses' rows='2'>{fv('weaknesses')}</textarea></div>
          <div><label>Any court convictions or outstanding hearings?</label>
            <textarea name='convictions' rows='2' placeholder='Please declare if any'>{fv('convictions')}</textarea></div>
          <div><label>Have you previously applied to or worked at Snappy Snaps?</label>
            <select name='prev_snappy'>
              <option value='No' {'selected' if fv('prev_snappy','No')=='No' else ''}>No</option>
              <option value='Yes' {'selected' if fv('prev_snappy')=='Yes' else ''}>Yes</option>
            </select></div>
          {fi('prev_snappy_details', 'If yes, please give details', placeholder='Store, position, dates')}
        </div>
      </div>

      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Employment History (most recent first)</div>"""

    for i in range(1, 4):
        content += f"""
        <div style='border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-bottom:10px'>
          <div style='font-size:12px;font-weight:700;color:#64748b;margin-bottom:8px;text-transform:uppercase'>Employer {i}</div>
          <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr))'>
            <div><label>Employer Name</label><input type='text' name='emp{i}_name' value='{fv(f"emp{i}_name")}'></div>
            <div><label>Address</label><input type='text' name='emp{i}_address' value='{fv(f"emp{i}_address")}'></div>
            <div><label>Date Commenced</label><input type='date' name='emp{i}_start' value='{fv(f"emp{i}_start")}'></div>
            <div><label>Date Left</label><input type='date' name='emp{i}_end' value='{fv(f"emp{i}_end")}'></div>
            <div><label>Position</label><input type='text' name='emp{i}_position' value='{fv(f"emp{i}_position")}'></div>
            <div><label>Salary</label><input type='text' name='emp{i}_salary' value='{fv(f"emp{i}_salary")}'></div>
            <div style='grid-column:1/-1'><label>Reason for leaving</label>
              <input type='text' name='emp{i}_reason' value='{fv(f"emp{i}_reason")}'></div>
          </div>
        </div>"""

    content += f"""
      </div>

      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>References</div>
        <div class='grid gap-6' style='grid-template-columns:1fr 1fr'>"""

    for i in range(1, 3):
        content += f"""
          <div>
            <div style='font-size:12px;font-weight:700;color:#64748b;margin-bottom:8px;text-transform:uppercase'>Reference {i}</div>
            <div class='space-y-2'>
              <div><label>Name</label><input type='text' name='ref{i}_name' value='{fv(f"ref{i}_name")}'></div>
              <div><label>Address</label><textarea name='ref{i}_address' rows='2'>{fv(f"ref{i}_address")}</textarea></div>
            </div>
          </div>"""

    content += f"""
        </div>
      </div>

      <div class='card' style='background:#fef3c7;border-color:#fcd34d'>
        <div style='font-size:13px;color:#92400e;font-weight:600;margin-bottom:12px'>
          Declaration: I warrant that the information given is complete, true and accurate.
          I understand that any false statement may disqualify me from employment.
        </div>
        <div class='grid gap-3' style='grid-template-columns:1fr 1fr'>
          {fi('declaration_name', 'Printed Name', val=f"{s.get('first_name','')} {s.get('last_name','')}", req=True)}
          {fi('declaration_date', 'Date', 'date', req=True)}
        </div>
      </div>

      <div style='display:flex;gap:8px'>
        <button type='submit' name='action' value='save' class='btn-secondary'>💾 Save Progress</button>
        <button type='submit' name='action' value='complete' class='btn-primary'>✅ Submit & Generate PDF</button>
        <a href='/staff/{staff_id}/onboarding' class='btn-secondary'>Cancel</a>
      </div>
    </form>"""

    return page("Employment Application", content, user, "staff")


@router.post("/staff/{staff_id}/onboarding/employment_application")
async def save_employment_application(
    staff_id: int,
    request:  Request,
    session:  str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    import json
    form   = await request.form()
    action = form.get("action","save")
    data   = {k: str(v) for k, v in form.items() if k != "action"}
    status = "completed" if action == "complete" else "in_progress"
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    q("""INSERT INTO onboarding_forms (staff_id, form_type, status, started_at, completed_at, form_data)
         VALUES(?,?,?,?,?,?)
         ON CONFLICT(staff_id,form_type) DO UPDATE SET
            status=excluded.status,
            completed_at=excluded.completed_at,
            form_data=excluded.form_data""",
      (staff_id, "employment_application", status, now, now if status=="completed" else None,
       json.dumps(data)))

    # Update staff profile with key fields
    q("""UPDATE staff_profiles SET phone=?, address_1=?
         WHERE staff_id=?""",
      (data.get("mobile") or data.get("phone",""),
       data.get("address",""), staff_id))

    from urllib.parse import quote as uq
    msg = "Application submitted ✅" if status=="completed" else "Progress saved"
    return RedirectResponse(
        f"/staff/{staff_id}/onboarding?msg={uq(msg)}", status_code=303)


@router.get("/staff/{staff_id}/onboarding/p46", response_class=HTMLResponse)
def p46_form(staff_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"

    saved = q("SELECT form_data FROM onboarding_forms WHERE staff_id=? AND form_type='p46'",
              (staff_id,), fetch=True)
    import json
    data  = json.loads(dict(saved[0])["form_data"]) if saved and dict(saved[0])["form_data"] else {}
    has_data = bool(data)
    def fv(k, d=""): return data.get(k, d) if has_data else d

    content = f"""
    <div>
      <a href='/staff/{staff_id}/onboarding' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to Onboarding</a>
      <div class='text-2xl font-black text-slate-800 mt-1'>P46 — Employee without a P45</div>
      <div style='font-size:13px;color:#64748b;margin-top:2px'>Section one — to be completed by the employee</div>
    </div>
    <form action='/staff/{staff_id}/onboarding/p46' method='POST' class='space-y-6'>
      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Your Details</div>
        <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>
          <div><label>Title</label>
            <select name='title'>
              <option>Mr</option><option>Mrs</option><option>Miss</option><option>Ms</option><option>Dr</option>
            </select></div>
          <div><label>Surname</label><input type='text' name='surname' value='{fv("surname", s.get("last_name",""))}' required></div>
          <div><label>First Name(s)</label><input type='text' name='first_name' value='{fv("first_name", s.get("first_name",""))}' required></div>
          <div><label>Gender</label>
            <select name='gender'>
              <option value='Male' {'selected' if fv('gender','Male')=='Male' else ''}>Male</option>
              <option value='Female' {'selected' if fv('gender')=='Female' else ''}>Female</option>
            </select></div>
          <div><label>Date of Birth</label><input type='date' name='dob' value='{fv("dob", s.get("date_of_birth",""))}' required></div>
          <div><label>National Insurance Number</label><input type='text' name='nino' value='{fv("nino")}' placeholder='AB 12 34 56 C' required></div>
          <div style='grid-column:1/-1'><label>Address</label>
            <input type='text' name='address' value='{fv("address", ", ".join(filter(None,[s.get("address_1",""),s.get("address_2",""),s.get("address_3",""),s.get("postcode","")])))}'></div>
        </div>
      </div>
      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Your Present Circumstances</div>
        <div style='font-size:13px;color:#64748b;margin-bottom:12px'>Please select the statement that applies to you:</div>
        <div class='space-y-3'>
          <label style='display:flex;gap:10px;align-items:flex-start;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
            <input type='radio' name='circumstance' value='A' {'checked' if fv('circumstance')=='A' else ''} style='width:auto;margin-top:3px'>
            <span><strong>A</strong> — This is my first job since last 6 April and I have not been receiving taxable Jobseeker's Allowance, Employment and Support Allowance or a state/occupational pension.</span>
          </label>
          <label style='display:flex;gap:10px;align-items:flex-start;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
            <input type='radio' name='circumstance' value='B' {'checked' if fv('circumstance')=='B' else ''} style='width:auto;margin-top:3px'>
            <span><strong>B</strong> — This is now my only job, but since last 6 April I have had another job or received taxable Jobseeker's Allowance or Employment Support Allowance.</span>
          </label>
          <label style='display:flex;gap:10px;align-items:flex-start;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
            <input type='radio' name='circumstance' value='C' {'checked' if fv('circumstance')=='C' else ''} style='width:auto;margin-top:3px'>
            <span><strong>C</strong> — I have another job or receive a state or occupational pension.</span>
          </label>
        </div>
        <div style='margin-top:12px'>
          <label style='display:flex;gap:10px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
            <input type='checkbox' name='student_loan' value='D' {'checked' if fv('student_loan')=='D' else ''} style='width:auto'>
            <span><strong>D</strong> — I have a Student Loan to repay (box D on P46)</span>
          </label>
        </div>
      </div>
      <div class='card' style='background:#fef3c7;border-color:#fcd34d'>
        <div style='font-size:13px;color:#92400e;font-weight:600;margin-bottom:10px'>
          Declaration: I confirm that this information is correct.
        </div>
        <div class='grid gap-3' style='grid-template-columns:1fr 1fr'>
          <div><label>Date</label><input type='date' name='sign_date' value='{fv("sign_date")}' required></div>
        </div>
      </div>

      {'"""' if user["role"] != "owner" else f"""
      <div class='card' style='border:2px solid #0f2942'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:4px'>Section 2 — To be completed by the Employer</div>
        <div style='font-size:12px;color:#94a3b8;margin-bottom:12px'>Owner only — not visible to staff</div>
        <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>
          <div><label>Employer Name &amp; Address</label>
            <select name='s2_employer'>
              <option value='Sappy Properties (Uxbridge) Llp T/A Snappy Snaps, 178 High Street, Uxbridge, Middlesex, UB8 1LA' {'selected' if fv('s2_employer','').startswith('Sappy') else ''}>
                Snappy Snaps Uxbridge — 178 High Street, Uxbridge UB8 1LA
              </option>
              <option value='Maukbs Ltd T/A Snappy Snaps, 95 Northbrook Street, Newbury, Berkshire, RG14 1AA' {'selected' if fv('s2_employer','').startswith('Maukbs') else ''}>
                Snappy Snaps Newbury — 95 Northbrook Street, Newbury RG14 1AA
              </option>
            </select>
          </div>
          <div><label>Date Employment Started</label>
            <input type='date' name='s2_start_date' value='{fv("s2_start_date") or s.get("date_joined","")}'></div>
          <div><label>Job Title</label>
            <input type='text' name='s2_job_title' value='{fv("s2_job_title") or s.get("job_title","")}'
              placeholder='e.g. Sales Assistant'></div>
          <div><label>Works/Payroll Number</label>
            <input type='text' name='s2_payroll_no' value='{fv("s2_payroll_no")}' placeholder='e.g. P001'></div>
          <div><label>Employer PAYE Reference</label>
            <input type='text' name='s2_paye_ref' value='{fv("s2_paye_ref")}' placeholder='e.g. 123/AB456'></div>
          <div><label>Tax Code Used</label>
            <input type='text' name='s2_tax_code' value='{fv("s2_tax_code")}' placeholder='e.g. 1257L'></div>
        </div>
        <div style='margin-top:12px'>
          <div style='font-size:12px;font-weight:700;color:#64748b;margin-bottom:8px;text-transform:uppercase'>Tax Code Basis</div>
          <div class='space-y-2'>
            <label style='display:flex;gap:8px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
              <input type='radio' name='s2_tax_basis' value='A_cumulative' {'checked' if fv("s2_tax_basis")=="A_cumulative" else ''} style='width:auto'>
              Box A — Emergency code on a cumulative basis
            </label>
            <label style='display:flex;gap:8px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
              <input type='radio' name='s2_tax_basis' value='B_week1' {'checked' if fv("s2_tax_basis")=="B_week1" else ''} style='width:auto'>
              Box B — Emergency code on a non-cumulative Week 1/Month 1 basis
            </label>
            <label style='display:flex;gap:8px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
              <input type='radio' name='s2_tax_basis' value='C_BR' {'checked' if fv("s2_tax_basis")=="C_BR" else ''} style='width:auto'>
              Box C — Code BR (or 0T if employee fails to complete Section 1) Week 1/Month 1 basis
            </label>
          </div>
        </div>
      </div>""" if user["role"] == "owner" else ""}

      <div style='display:flex;gap:8px'>
        <button type='submit' name='action' value='save' class='btn-secondary'>💾 Save Progress</button>
        <button type='submit' name='action' value='complete' class='btn-primary'>✅ Submit</button>
        <a href='/staff/{staff_id}/onboarding' class='btn-secondary'>Cancel</a>
      </div>
    </form>"""

    return page("P46", content, user, "staff")


@router.post("/staff/{staff_id}/onboarding/p46")
async def save_p46(staff_id: int, request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r
    import json
    form   = await request.form()
    action = form.get("action","save")
    data   = {k: str(v) for k, v in form.items() if k != "action"}
    status = "completed" if action == "complete" else "in_progress"
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    q("""INSERT INTO onboarding_forms (staff_id,form_type,status,started_at,completed_at,form_data)
         VALUES(?,?,?,?,?,?)
         ON CONFLICT(staff_id,form_type) DO UPDATE SET
            status=excluded.status, completed_at=excluded.completed_at, form_data=excluded.form_data""",
      (staff_id,"p46",status,now,now if status=="completed" else None, json.dumps(data)))
    # Update DOB on profile
    if data.get("dob"):
        q("UPDATE staff_profiles SET date_of_birth=? WHERE staff_id=?", (data["dob"], staff_id))
    from urllib.parse import quote as uq
    return RedirectResponse(f"/staff/{staff_id}/onboarding?msg={uq('P46 saved')}", status_code=303)


@router.get("/staff/{staff_id}/onboarding/new_employee_notify", response_class=HTMLResponse)
def new_employee_notify_form(staff_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] != "owner":
        return RedirectResponse(f"/staff/{staff_id}/onboarding", status_code=303)
    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows: return RedirectResponse("/staff", status_code=303)
    s    = dict(rows[0])
    name = f"{s['first_name']} {s['last_name']}"

    saved = q("SELECT form_data FROM onboarding_forms WHERE staff_id=? AND form_type='new_employee_notify'",
              (staff_id,), fetch=True)
    import json
    data     = json.loads(dict(saved[0])["form_data"]) if saved and dict(saved[0])["form_data"] else {}
    has_data = bool(data)
    def fv(k, d=""): return data.get(k, d) if has_data else d

    def fi(nm, lbl, ft="text", val=None, ph=""):
        v  = val if val is not None else fv(nm)
        ph = f"placeholder='{ph}'" if ph else ""
        return f"<div><label>{lbl}</label><input type='{ft}' name='{nm}' value='{v}' {ph}></div>"

    content = f"""
    <div>
      <a href='/staff/{staff_id}/onboarding' style='color:#1e3a5f;font-size:13px;font-weight:700'>← Back to Onboarding</a>
      <div class='text-2xl font-black text-slate-800 mt-1'>New Employee Notification — {name}</div>
      <div style='font-size:12px;color:#94a3b8;margin-top:2px'>Owner only — not visible to staff</div>
    </div>
    <form action='/staff/{staff_id}/onboarding/new_employee_notify' method='POST' class='space-y-6'>
      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Employee Details</div>
        <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>
          {fi('surname',      'Surname',       val=fv('surname') or s.get('last_name',''))}
          {fi('first_name',   'First Name(s)', val=fv('first_name') or s.get('first_name',''))}
          <div><label>Title</label><select name='title'><option>Mr</option><option>Mrs</option><option>Miss</option><option>Ms</option></select></div>
          <div><label>Gender</label><select name='gender'><option>Male</option><option>Female</option></select></div>
          <div><label>Married</label><select name='married'><option value='No'>No</option><option value='Yes' {'selected' if fv('married')=='Yes' else ''}>Yes</option></select></div>
          {fi('dob',          'Date of Birth', 'date', s.get('date_of_birth',''))}
          {fi('nino',         'NI Number',     ph='AB 12 34 56 C')}
          {fi('start_date',   'Start Date',    'date', s.get('date_joined',''))}
          {fi('address',      'Employee Address', val=fv('address') or ', '.join(filter(None,[s.get('address_1',''),s.get('address_2',''),s.get('address_3',''),s.get('postcode','')])))}
          {fi('postcode',     'Post Code',     val=fv('postcode') or s.get('postcode',''))}
          {fi('phone',        'Phone',         val=fv('phone') or s.get('phone',''))}
          {fi('emergency',    'Emergency Contact')}
        </div>
      </div>
      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Employment Details (Employer)</div>
        <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>
          <div><label>Employer Name & Address</label>
            <select name='employer_name'>
              <option value='Sappy Properties (Uxbridge) Llp T/A Snappy Snaps, 178 High Street, Uxbridge, Middlesex, UB8 1LA' {'selected' if "Uxbridge" in fv("employer_name","") else ""}>
                Snappy Snaps Uxbridge — 178 High Street, Uxbridge, Middlesex UB8 1LA
              </option>
              <option value='Maukbs Ltd T/A Snappy Snaps, 95 Northbrook Street, Newbury, Berkshire, RG14 1AA' {'selected' if "Newbury" in fv("employer_name","") else ""}>
                Snappy Snaps Newbury — 95 Northbrook Street, Newbury, Berkshire RG14 1AA
              </option>
            </select>
          </div>
          <div><label>Pay Frequency</label>
            <select name='pay_frequency'>
              <option {'selected' if fv('pay_frequency','Monthly')=='Monthly' else ''}>Monthly</option>
              <option {'selected' if fv('pay_frequency')=='Weekly' else ''}>Weekly</option>
              <option {'selected' if fv('pay_frequency')=='4 Weekly' else ''}>4 Weekly</option>
            </select></div>
          {fi('pay_day',       'Pay Day & Date', ph='e.g. Last Friday of month')}
          <div><label>Pay Method</label>
            <select name='pay_method'>
              <option {'selected' if fv('pay_method','BACS')=='BACS' else ''}>BACS</option>
              <option {'selected' if fv('pay_method')=='Cash' else ''}>Cash</option>
              <option {'selected' if fv('pay_method')=='Cheque' else ''}>Cheque</option>
            </select></div>
          {fi('payroll_no',    'Payroll No.',    ph='e.g. P001')}
          {fi('tax_code',      'Tax Code',       ph='e.g. 1257L')}
          {fi('nic_letter',    'NIC Letter',     ph='e.g. A')}
          {fi('contracted_hrs','Contracted Hours/Week', val=fv('contracted_hrs') or str(s.get('contracted_hrs','')))}
          {fi('wage',          'Wage/Salary',    ph='e.g. £12.71 per hour')}
          {fi('holiday_start', 'Holiday Year Start', 'date', fv('holiday_start','2026-01-01'))}
          {fi('holiday_end',   'Holiday Year End',   'date', fv('holiday_end','2026-12-31'))}
          {fi('holiday_days',  'Holiday Entitlement (days)', ph='e.g. 19')}
          <div><label>Employment Type</label>
            <select name='emp_type'>
              <option {'selected' if fv('emp_type','Permanent')=='Permanent' else ''}>Permanent</option>
              <option {'selected' if fv('emp_type')=='Temporary' else ''}>Temporary</option>
            </select></div>
          <div><label>Student?</label>
            <select name='is_student'>
              <option value='No' {'selected' if fv('is_student','No')=='No' else ''}>No</option>
              <option value='Yes' {'selected' if fv('is_student')=='Yes' else ''}>Yes</option>
            </select></div>
          <div><label>Only Employment?</label>
            <select name='only_employment'>
              <option value='Yes' {'selected' if fv('only_employment','Yes')=='Yes' else ''}>Yes</option>
              <option value='No' {'selected' if fv('only_employment')=='No' else ''}>No</option>
            </select></div>
        </div>
      </div>
      <div class='card'>
        <div style='font-weight:900;color:#0f2942;margin-bottom:12px'>Right to Work Documents Checked</div>
        <div class='space-y-2'>
          <label style='display:flex;gap:8px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
            <input type='checkbox' name='rtw_passport' value='1' {'checked' if fv('rtw_passport') else ''} style='width:auto'>
            UK or EEA Passport
          </label>
          <label style='display:flex;gap:8px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
            <input type='checkbox' name='rtw_birth_cert' value='1' {'checked' if fv('rtw_birth_cert') else ''} style='width:auto'>
            Full British Birth Certificate
          </label>
          <label style='display:flex;gap:8px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:400'>
            <input type='checkbox' name='rtw_work_permit' value='1' {'checked' if fv('rtw_work_permit') else ''} style='width:auto'>
            Work Permit with Passport
          </label>
        </div>
      </div>
      <div style='display:flex;gap:8px'>
        <button type='submit' name='action' value='save' class='btn-secondary'>💾 Save Progress</button>
        <button type='submit' name='action' value='complete' class='btn-primary'>✅ Mark Complete</button>
        <a href='/staff/{staff_id}/onboarding' class='btn-secondary'>Cancel</a>
      </div>
    </form>"""

    return page("New Employee Notification", content, user, "staff")


@router.post("/staff/{staff_id}/onboarding/new_employee_notify")
async def save_new_employee_notify(
    staff_id: int, request: Request, session: str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _require_mgr(user)): return r
    import json
    form   = await request.form()
    action = form.get("action","save")
    data   = {k: str(v) for k, v in form.items() if k != "action"}
    status = "completed" if action == "complete" else "in_progress"
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    q("""INSERT INTO onboarding_forms (staff_id,form_type,status,started_at,completed_at,form_data)
         VALUES(?,?,?,?,?,?)
         ON CONFLICT(staff_id,form_type) DO UPDATE SET
            status=excluded.status, completed_at=excluded.completed_at, form_data=excluded.form_data""",
      (staff_id,"new_employee_notify",status,now,now if status=="completed" else None,json.dumps(data)))
    from urllib.parse import quote as uq
    return RedirectResponse(f"/staff/{staff_id}/onboarding?msg={uq('Notification saved')}", status_code=303)


@router.post("/staff/{staff_id}/onboarding/{form_type}/upload-paper")
async def upload_paper_form(
    staff_id:  int,
    form_type: str,
    request:   Request,
    session:   str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if (r := _staff_access_guard(user, staff_id)): return r

    form      = await request.form()
    paper     = form.get("paper_form")

    if not paper or not hasattr(paper, "filename") or not paper.filename:
        from urllib.parse import quote as uq
        return RedirectResponse(
            f"/staff/{staff_id}/onboarding?msg={uq('No file selected')}&msg_type=error",
            status_code=303)

    # Save the file (sanitise name parts to prevent path traversal; whitelist ext; cap size)
    ext      = os.path.splitext(paper.filename)[1].lower()
    filename = f"onboard_{staff_id}_{_safe_part(form_type)}_paper{_safe_ext(ext)}"
    filepath = os.path.join(DOCS_DIR, filename)
    data = await paper.read()
    if len(data) > 25 * 1024 * 1024:
        from urllib.parse import quote as uq
        return RedirectResponse(f"/staff/{staff_id}/onboarding?msg={uq('File too large (max 25 MB)')}&msg_type=error",
                                status_code=303)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(data)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Mark form as completed with paper upload
    q("""INSERT INTO onboarding_forms
            (staff_id, form_type, status, started_at, completed_at, form_data, pdf_path)
         VALUES(?,?,?,?,?,?,?)
         ON CONFLICT(staff_id,form_type) DO UPDATE SET
            status='completed',
            completed_at=excluded.completed_at,
            pdf_path=excluded.pdf_path""",
      (staff_id, form_type, "completed", now, now,
       '{"source":"paper_upload"}', filepath))

    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/staff/{staff_id}/onboarding?msg={uq('Paper form uploaded and marked complete')}",
        status_code=303)


ensure_staff_tables()
ensure_onboarding_tables()
