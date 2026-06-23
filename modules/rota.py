"""rota routes."""
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


def ensure_rota_tables():
    conn = db()
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS rota_templates (
            template_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id     INTEGER NOT NULL,
            store_name   TEXT NOT NULL,
            day_of_week  INTEGER NOT NULL,
            shift_start  TEXT,
            shift_end    TEXT,
            hours        REAL,
            is_off       INTEGER DEFAULT 0,
            UNIQUE(staff_id, day_of_week),
            FOREIGN KEY (staff_id) REFERENCES staff_profiles(staff_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rotas (
            rota_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name   TEXT NOT NULL,
            week_start   TEXT NOT NULL,
            status       TEXT DEFAULT 'draft',
            published_at TEXT,
            published_by TEXT,
            notes        TEXT,
            UNIQUE(store_name, week_start)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS rota_shifts (
            shift_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            rota_id      INTEGER NOT NULL,
            staff_id     INTEGER NOT NULL,
            shift_date   TEXT NOT NULL,
            shift_start  TEXT,
            shift_end    TEXT,
            hours        REAL,
            is_off       INTEGER DEFAULT 0,
            absence_type TEXT,
            notes        TEXT,
            UNIQUE(rota_id, staff_id, shift_date),
            FOREIGN KEY (rota_id)  REFERENCES rotas(rota_id),
            FOREIGN KEY (staff_id) REFERENCES staff_profiles(staff_id)
        )
    """)
    conn.commit()
    conn.close()


def get_or_create_rota(store_name: str, week_start: str) -> dict:
    """Get existing rota or create from templates."""
    rows = q("SELECT * FROM rotas WHERE store_name=? AND week_start=?",
             (store_name, week_start), fetch=True)
    if rows:
        rota = dict(rows[0])
        # Load shifts
        shifts = q("""SELECT rs.*, sp.first_name, sp.last_name
                      FROM rota_shifts rs
                      JOIN staff_profiles sp ON rs.staff_id=sp.staff_id
                      WHERE rs.rota_id=?""",
                   (rota["rota_id"],), fetch=True) or []
        rota["shifts"] = [dict(s) for s in shifts]
        return rota

    # Create new rota from templates
    q("INSERT OR IGNORE INTO rotas (store_name, week_start, status) VALUES(?,?,'draft')",
      (store_name, week_start))
    rota_rows = q("SELECT * FROM rotas WHERE store_name=? AND week_start=?",
                  (store_name, week_start), fetch=True)
    rota     = dict(rota_rows[0])
    rota_id  = rota["rota_id"]
    week_dates = get_week_dates(week_start)

    # Get active staff for this store
    staff = q("SELECT * FROM staff_profiles WHERE store_name=? AND is_active=1",
              (store_name,), fetch=True) or []

    # Get approved leave for this week
    leave_map = {}
    for s in staff:
        leaves = q("""SELECT date_from, date_to, leave_type FROM leave_requests
                      WHERE staff_id=? AND status='approved'
                        AND date_from <= ? AND date_to >= ?""",
                   (s["staff_id"], week_dates[-1], week_dates[0]), fetch=True) or []
        for lv in leaves:
            lv = dict(lv)
            d1 = datetime.strptime(lv["date_from"], "%Y-%m-%d")
            d2 = datetime.strptime(lv["date_to"],   "%Y-%m-%d")
            cur = d1
            while cur <= d2:
                leave_map[(s["staff_id"], cur.strftime("%Y-%m-%d"))] = lv["leave_type"]
                cur += timedelta(days=1)

    # Build shifts from templates
    for s in staff:
        sid = s["staff_id"]
        templates = q("SELECT * FROM rota_templates WHERE staff_id=?", (sid,), fetch=True) or []
        tmpl_map  = {dict(t)["day_of_week"]: dict(t) for t in templates}

        for i, date_str in enumerate(week_dates):
            dow      = i  # 0=Sun
            tmpl     = tmpl_map.get(dow, {})
            leave_type = leave_map.get((sid, date_str))

            if leave_type:
                q("""INSERT OR IGNORE INTO rota_shifts
                        (rota_id, staff_id, shift_date, is_off, absence_type)
                     VALUES(?,?,?,1,?)""",
                  (rota_id, sid, date_str, leave_type))
            elif tmpl.get("is_off", 1):
                q("""INSERT OR IGNORE INTO rota_shifts
                        (rota_id, staff_id, shift_date, is_off)
                     VALUES(?,?,?,1)""",
                  (rota_id, sid, date_str))
            else:
                q("""INSERT OR IGNORE INTO rota_shifts
                        (rota_id, staff_id, shift_date,
                         shift_start, shift_end, hours, is_off)
                     VALUES(?,?,?,?,?,?,0)""",
                  (rota_id, sid, date_str,
                   tmpl.get("shift_start"), tmpl.get("shift_end"),
                   tmpl.get("hours")))

    return get_or_create_rota(store_name, week_start)


@router.get("/rota", response_class=HTMLResponse)
def rota_page(
    session:    str | None = Cookie(default=None),
    store:      str = "Uxbridge",
    week_start: str = "",
    msg:        str = ""
):
    redir, user = require_login(session)
    if redir: return redir

    # Store access control
    if user["role"] == "staff" and user.get("store_name"):
        store = user["store_name"]
    elif user["role"] == "manager" and user.get("store_name") and not store:
        store = user["store_name"]

    if not week_start:
        week_start = get_week_start()

    week_dates  = get_week_dates(week_start)
    prev_week   = (datetime.strptime(week_start, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week   = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
    week_end    = week_dates[-1]
    is_mgr      = user["role"] in ("owner","manager")
    flash       = f"<div class='flash-success'>{msg}</div>" if msg else ""

    rota = get_or_create_rota(store, week_start)
    rota_id = rota["rota_id"]
    status  = rota["status"]

    # Build shift lookup: (staff_id, date) → shift
    shift_lookup = {(s["staff_id"], s["shift_date"]): s for s in rota.get("shifts",[])}

    # Get active staff for this store
    staff = q("SELECT * FROM staff_profiles WHERE store_name=? AND is_active=1 ORDER BY first_name",
              (store,), fetch=True) or []

    # Status badge
    status_colours = {
        "draft":     ("#fef3c7","#92400e","Draft"),
        "published": ("#dcfce7","#166534","Published"),
        "locked":    ("#dbeafe","#1e40af","Locked"),
    }
    sc = status_colours.get(status, ("#f1f5f9","#64748b","Unknown"))
    status_badge = f"<span style='background:{sc[0]};color:{sc[1]};font-size:12px;font-weight:700;padding:3px 10px;border-radius:6px'>{sc[2]}</span>"

    # Store switcher (managers/owners only)
    store_switcher = ""
    if user["role"] in ("owner","manager"):
        for sv in ["Uxbridge","Newbury"]:
            cls = "btn-primary" if sv == store else "btn-secondary"
            store_switcher += f"<a href='/rota?store={sv}&week_start={week_start}' class='{cls}' style='padding:5px 14px;font-size:13px'>{sv}</a>"

    # Action buttons
    action_btns = ""
    if is_mgr:
        if status == "draft":
            action_btns += f"<a href='/rota/publish?store={store}&week_start={week_start}' class='btn-primary' style='padding:6px 16px;font-size:13px'>&#128228; Publish Rota</a>"
        elif status == "published":
            action_btns += f"<a href='/rota/unpublish?store={store}&week_start={week_start}' class='btn-secondary' style='padding:6px 16px;font-size:13px'>&#128221; Back to Draft</a>"
        action_btns += f"<a href='/rota/pdf?store={store}&week_start={week_start}' class='btn-secondary' style='padding:6px 16px;font-size:13px'>&#128196; Download PDF</a>"
        action_btns += f"<a href='/rota/templates?store={store}' class='btn-secondary' style='padding:6px 16px;font-size:13px'>&#9881;&#65039; Templates</a>"

    # Build rota grid
    # Header row
    header = "<tr style='background:#0f2942;color:white'>"
    header += "<th style='padding:10px 12px;text-align:left;font-size:12px;min-width:140px'>Staff</th>"
    for i, date_str in enumerate(week_dates):
        d    = datetime.strptime(date_str, "%Y-%m-%d")
        day  = DAYS[i]
        date = d.strftime("%d %b")
        is_today = date_str == datetime.now().strftime("%Y-%m-%d")
        bg = "background:#1e3a5f" if is_today else ""
        header += f"<th style='padding:8px 6px;text-align:center;font-size:11px;min-width:90px;{bg}'>{day}<br><span style='font-size:10px;opacity:.7'>{date}</span></th>"
    header += "<th style='padding:10px 8px;font-size:11px;text-align:center'>Hrs</th></tr>"

    # Staff rows
    grid_rows = ""
    for s in staff:
        sid  = s["staff_id"]
        name = f"{s['first_name']} {s['last_name']}"
        total_hrs = 0

        grid_rows += f"<tr style='border-bottom:1px solid #f1f5f9'>"
        grid_rows += f"<td style='padding:8px 12px;font-weight:700;font-size:13px;color:#0f172a;white-space:nowrap'>{name}</td>"

        for i, date_str in enumerate(week_dates):
            shift = shift_lookup.get((sid, date_str), {})
            is_off      = shift.get("is_off", 1)
            absence     = shift.get("absence_type")
            start       = shift.get("shift_start") or ""
            end         = shift.get("shift_end") or ""
            hrs         = shift.get("hours") or 0
            shift_id    = shift.get("shift_id","")
            if hrs: total_hrs += hrs

            if absence:
                absence_colours = {
                    "H": ("#dcfce7","#166534","Holiday"),
                    "S": ("#fee2e2","#dc2626","Sick"),
                    "B": ("#fef3c7","#92400e","BH"),
                    "AL":("#dbeafe","#1d4ed8","Auth.Leave"),
                }
                ac = absence_colours.get(absence, ("#f1f5f9","#64748b", absence))
                cell = f"<div style='background:{ac[0]};color:{ac[1]};border-radius:6px;padding:4px 6px;font-size:11px;font-weight:700;text-align:center'>{ac[2]}</div>"
            elif is_off:
                cell = "<div style='color:#cbd5e1;font-size:12px;text-align:center'>OFF</div>"
            else:
                cell = f"<div style='background:#eff6ff;border-radius:6px;padding:4px 6px;text-align:center'>"
                cell += f"<div style='font-size:12px;font-weight:700;color:#1e40af'>{start}–{end}</div>"
                if hrs: cell += f"<div style='font-size:10px;color:#64748b'>{hrs}h</div>"
                cell += "</div>"

            # Editable if draft/published and manager
            if is_mgr and status in ("draft","published"):
                cell = f"<a href='/rota/edit-shift?rota_id={rota_id}&staff_id={sid}&date={date_str}' style='text-decoration:none;display:block'>{cell}</a>"

            grid_rows += f"<td style='padding:4px 3px'>{cell}</td>"

        hrs_str = f"<span style='font-size:12px;font-weight:700;color:#0f172a'>{total_hrs:.1f}</span>" if total_hrs else "—"
        grid_rows += f"<td style='padding:4px 8px;text-align:center'>{hrs_str}</td>"
        grid_rows += "</tr>"

    # Weekly totals row — hours and staff count per day
    day_totals_hrs   = []
    day_totals_count = []
    week_total  = 0
    week_staff  = set()

    for date_str in week_dates:
        day_hrs   = 0
        day_count = 0
        for s in staff:
            sid   = dict(s)["staff_id"]
            shift = shift_lookup.get((sid, date_str), {})
            hrs   = shift.get("hours") or 0
            if hrs and not shift.get("is_off") and not shift.get("absence_type"):
                day_hrs   += hrs
                day_count += 1
                week_staff.add(sid)
        week_total += day_hrs

        day_totals_hrs.append(
            f"<td style='padding:4px 3px;text-align:center;background:#f8fafc;border-right:1px solid #e2e8f0'>"
            f"<div style='font-size:12px;font-weight:700;color:#0f2942'>{day_hrs:.1f}h</div>"
            f"<div style='font-size:10px;color:#64748b;margin-top:1px'>{day_count} staff</div>"
            f"</td>"
        )

    totals_row = "<tr style='border-top:2px solid #e2e8f0'>"
    totals_row += "<td style='padding:8px 12px;font-size:11px;font-weight:700;color:#64748b;background:#f8fafc'>TOTALS</td>"
    totals_row += "".join(day_totals_hrs)
    totals_row += (
        f"<td style='padding:8px;text-align:center;background:#f8fafc'>"
        f"<div style='font-size:13px;font-weight:900;color:#0f2942'>{week_total:.1f}h</div>"
        f"<div style='font-size:10px;color:#64748b'>week</div>"
        f"</td>"
    )
    totals_row += "</tr>"

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <div class='text-2xl font-black text-slate-800'>&#128197; Rota — {store}</div>
        <div style='font-size:13px;color:#64748b;margin-top:2px'>
          Week: {datetime.strptime(week_start,"%Y-%m-%d").strftime("%d %b")} – {datetime.strptime(week_end,"%Y-%m-%d").strftime("%d %b %Y")}
          &nbsp; {status_badge}
        </div>
      </div>
      <div style='display:flex;gap:8px;flex-wrap:wrap;align-items:center'>
        {store_switcher}
        <a href='/rota?store={store}&week_start={prev_week}' class='btn-secondary' style='padding:5px 12px'>&#8592; Prev</a>
        <a href='/rota?store={store}&week_start={get_week_start()}' class='btn-secondary' style='padding:5px 12px'>Today</a>
        <a href='/rota?store={store}&week_start={next_week}' class='btn-secondary' style='padding:5px 12px'>Next &#8594;</a>
        {action_btns}
      </div>
    </div>

    <!-- Legend -->
    <div style='display:flex;gap:12px;flex-wrap:wrap;font-size:12px;font-weight:600'>
      <span><span style='display:inline-block;width:12px;height:12px;background:#eff6ff;border-radius:3px;vertical-align:middle'></span> Working</span>
      <span><span style='display:inline-block;width:12px;height:12px;background:#f1f5f9;border-radius:3px;vertical-align:middle'></span> OFF</span>
      <span><span style='display:inline-block;width:12px;height:12px;background:#dcfce7;border-radius:3px;vertical-align:middle'></span> Holiday</span>
      <span><span style='display:inline-block;width:12px;height:12px;background:#fee2e2;border-radius:3px;vertical-align:middle'></span> Sick</span>
      <span><span style='display:inline-block;width:12px;height:12px;background:#fef3c7;border-radius:3px;vertical-align:middle'></span> Bank Holiday</span>
      {'<span style="color:#64748b;font-size:11px">Click any cell to edit shift</span>' if is_mgr and status in ("draft","published") else ''}
    </div>

    <div class='card' style='padding:0;overflow:hidden'>
      <div style='overflow-x:auto'>
        <table style='width:100%;border-collapse:collapse;font-family:DM Sans,sans-serif'>
          <thead>{header}</thead>
          <tbody>{grid_rows}{totals_row}</tbody>
        </table>
      </div>
    </div>"""

    return page("Rota", content, user, "rota")


@router.get("/rota/edit-shift", response_class=HTMLResponse)
def edit_shift_form(
    rota_id:  int = 0,
    staff_id: int = 0,
    date:     str = "",
    session:  str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse("/rota", status_code=303)

    # Get rota info
    rota = q("SELECT * FROM rotas WHERE rota_id=?", (rota_id,), fetch=True)
    if not rota: return RedirectResponse("/rota", status_code=303)
    rota = dict(rota[0])

    # Get staff info
    staff = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not staff: return RedirectResponse("/rota", status_code=303)
    s    = dict(staff[0])
    name = f"{s['first_name']} {s['last_name']}"

    # Get existing shift
    shift = q("SELECT * FROM rota_shifts WHERE rota_id=? AND staff_id=? AND shift_date=?",
              (rota_id, staff_id, date), fetch=True)
    sh = dict(shift[0]) if shift else {}

    d_fmt = datetime.strptime(date, "%Y-%m-%d").strftime("%A %d %B %Y")
    store = rota["store_name"]
    back  = f"/rota?store={store}&week_start={rota['week_start']}"

    absence_opts = "".join(
        f"<option value='{k}' {'selected' if sh.get('absence_type')==k else ''}>{v}</option>"
        for k,v in ABSENCE_TYPES.items()
    )

    content = f"""
    <div>
      <a href='{back}' style='color:#1e3a5f;font-size:13px;font-weight:700'>&#8592; Back to Rota</a>
      <div class='text-2xl font-black text-slate-800 mt-1'>Edit Shift — {name}</div>
      <div style='font-size:13px;color:#64748b'>{d_fmt} &middot; {store}</div>
    </div>
    <div class='card' style='max-width:480px'>
      <form action='/rota/save-shift' method='POST' class='space-y-4'>
        <input type='hidden' name='rota_id'  value='{rota_id}'>
        <input type='hidden' name='staff_id' value='{staff_id}'>
        <input type='hidden' name='date'     value='{date}'>
        <input type='hidden' name='store'    value='{store}'>
        <input type='hidden' name='week_start' value='{rota["week_start"]}'>

        <div>
          <label>Shift Type</label>
          <select name='shift_type' id='shift_type' onchange='toggleFields()'>
            <option value='working' {'selected' if not sh.get('is_off') and not sh.get('absence_type') else ''}>Working</option>
            <option value='off' {'selected' if sh.get('is_off') and not sh.get('absence_type') else ''}>OFF</option>
            <option value='absence' {'selected' if sh.get('absence_type') else ''}>Absence / Leave</option>
          </select>
        </div>

        <div id='working_fields' style='display:{"block" if not sh.get("is_off") and not sh.get("absence_type") else "none"}'>
          <div class='grid gap-3' style='grid-template-columns:1fr 1fr'>
            <div><label>Start Time</label>
              <input type='time' name='shift_start' value='{sh.get("shift_start") or "09:00"}'></div>
            <div><label>End Time</label>
              <input type='time' name='shift_end' value='{sh.get("shift_end") or "17:00"}'></div>
          </div>
          <div style='margin-top:8px'><label>Total Hours (optional — auto-calculates)</label>
            <input type='number' step='0.25' name='hours' id='hours_field'
                   value='{sh.get("hours") or ""}' placeholder='e.g. 7.5'></div>
        </div>

        <div id='absence_fields' style='display:{"block" if sh.get("absence_type") else "none"}'>
          <label>Absence Type</label>
          <select name='absence_type'>
            <option value=''>-- Select --</option>
            {absence_opts}
          </select>
        </div>

        <div><label>Notes (optional)</label>
          <input type='text' name='notes' value='{sh.get("notes") or ""}' placeholder='e.g. Cover for Jessica'></div>

        <div style='display:flex;gap:8px'>
          <button type='submit' class='btn-primary'>&#128190; Save Shift</button>
          <a href='{back}' class='btn-secondary'>Cancel</a>
        </div>
      </form>
    </div>
    <script>
    function toggleFields() {{
      const type = document.getElementById('shift_type').value;
      document.getElementById('working_fields').style.display = type==='working' ? 'block' : 'none';
      document.getElementById('absence_fields').style.display = type==='absence' ? 'block' : 'none';
    }}
    // Auto-calc hours from times
    document.addEventListener('DOMContentLoaded', function() {{
      const start = document.querySelector('[name="shift_start"]');
      const end   = document.querySelector('[name="shift_end"]');
      const hrs   = document.getElementById('hours_field');
      function calc() {{
        if (start.value && end.value) {{
          const [sh,sm] = start.value.split(':').map(Number);
          const [eh,em] = end.value.split(':').map(Number);
          const diff = (eh*60+em - sh*60-sm) / 60;
          if (diff > 0) hrs.value = diff.toFixed(2);
        }}
      }}
      if (start) start.addEventListener('change', calc);
      if (end)   end.addEventListener('change', calc);
    }});
    </script>"""

    return page("Edit Shift", content, user, "rota")


@router.post("/rota/save-shift")
async def save_shift(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    form        = await request.form()
    rota_id     = int(form.get("rota_id", 0))
    staff_id    = int(form.get("staff_id", 0))
    date        = form.get("date","")
    store       = form.get("store","")
    week_start  = form.get("week_start","")
    shift_type  = form.get("shift_type","off")
    shift_start = form.get("shift_start","") or None
    shift_end   = form.get("shift_end","")   or None
    notes       = form.get("notes","")       or None
    absence     = form.get("absence_type","") or None
    try:
        hrs = float(form.get("hours",0) or 0)
    except:
        hrs = 0

    is_off = 1 if shift_type != "working" else 0

    q("""INSERT INTO rota_shifts
            (rota_id, staff_id, shift_date, shift_start, shift_end,
             hours, is_off, absence_type, notes)
         VALUES(?,?,?,?,?,?,?,?,?)
         ON CONFLICT(rota_id, staff_id, shift_date) DO UPDATE SET
            shift_start=excluded.shift_start,
            shift_end=excluded.shift_end,
            hours=excluded.hours,
            is_off=excluded.is_off,
            absence_type=excluded.absence_type,
            notes=excluded.notes""",
      (rota_id, staff_id, date, shift_start, shift_end,
       hrs, is_off, absence if shift_type=="absence" else None, notes))

    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/rota?store={store}&week_start={week_start}&msg={uq('Shift saved')}",
        status_code=303)


@router.get("/rota/publish")
def publish_rota(
    store:      str = "",
    week_start: str = "",
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse("/rota", status_code=303)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    q("""UPDATE rotas SET status='published', published_at=?, published_by=?
         WHERE store_name=? AND week_start=?""",
      (now, user.get("username"), store, week_start))
    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/rota?store={store}&week_start={week_start}&msg={uq('Rota published — staff can now see their shifts')}",
        status_code=303)


@router.get("/rota/unpublish")
def unpublish_rota(
    store:      str = "",
    week_start: str = "",
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    q("UPDATE rotas SET status='draft' WHERE store_name=? AND week_start=?",
      (store, week_start))
    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/rota?store={store}&week_start={week_start}&msg={uq('Rota moved back to draft')}",
        status_code=303)


@router.get("/rota/whatsapp", response_class=HTMLResponse)
def rota_whatsapp(
    store:      str = "",
    week_start: str = "",
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir

    week_dates = get_week_dates(week_start)
    week_end   = week_dates[-1]
    rota = get_or_create_rota(store, week_start)
    shift_lookup = {(s["staff_id"], s["shift_date"]): s for s in rota.get("shifts",[])}
    staff = q("SELECT * FROM staff_profiles WHERE store_name=? AND is_active=1 ORDER BY first_name",
              (store,), fetch=True) or []

    lines = [
        f"📅 *{store} Rota*",
        f"*Week: {datetime.strptime(week_start,'%Y-%m-%d').strftime('%d %b')} – {datetime.strptime(week_end,'%Y-%m-%d').strftime('%d %b %Y')}*",
        ""
    ]

    for s in staff:
        sid   = s["staff_id"]
        name  = s["first_name"]
        shifts = []
        for i, date_str in enumerate(week_dates):
            sh = shift_lookup.get((sid, date_str), {})
            if sh.get("absence_type"):
                at = ABSENCE_TYPES.get(sh["absence_type"], sh["absence_type"])
                shifts.append(f"{DAYS[i]}: {at}")
            elif not sh.get("is_off", 1):
                start = sh.get("shift_start","")
                end   = sh.get("shift_end","")
                shifts.append(f"{DAYS[i]}: {start}–{end}")
        if shifts:
            lines.append(f"*{name}*")
            lines.append("  " + " | ".join(shifts))
        else:
            lines.append(f"*{name}*: All OFF")
        lines.append("")

    message = "\n".join(lines)

    content = f"""
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>&#128242; WhatsApp Rota — {store}</div>
      <a href='/rota?store={store}&week_start={week_start}' class='btn-secondary'>&#8592; Back to Rota</a>
    </div>
    <div class='card'>
      <pre id='rota_text' style='white-space:pre-wrap;font-family:DM Mono,monospace;
           font-size:13px;line-height:1.6;background:#f8fafc;padding:16px;
           border-radius:10px;border:1px solid #e2e8f0'>{message}</pre>
      <button onclick='copyRota()'
        class='btn-primary' style='margin-top:12px;width:100%;padding:10px'>
        &#128203; Copy to Clipboard — then paste into WhatsApp
      </button>
    </div>
    <script>
    function copyRota() {{
      const text = document.getElementById('rota_text').textContent;
      navigator.clipboard.writeText(text).then(() => alert('&#10003; Copied! Open WhatsApp and paste.'));
    }}
    </script>"""

    return page("WhatsApp Rota", content, user, "rota")


@router.get("/rota/templates", response_class=HTMLResponse)
def rota_templates(
    store:   str = "Uxbridge",
    session: str | None = Cookie(default=None),
    msg:     str = ""
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse("/rota", status_code=303)

    staff = q("SELECT * FROM staff_profiles WHERE store_name=? AND is_active=1 ORDER BY first_name",
              (store,), fetch=True) or []
    flash = f"<div class='flash-success'>{msg}</div>" if msg else ""

    store_btns = ""
    for sv in ["Uxbridge","Newbury"]:
        cls = "btn-primary" if sv == store else "btn-secondary"
        store_btns += f"<a href='/rota/templates?store={sv}' class='{cls}' style='padding:5px 14px;font-size:13px'>{sv}</a>"

    # Build template grid
    header = "<tr style='background:#0f2942;color:white'>"
    header += "<th style='padding:10px 12px;text-align:left;font-size:12px'>Staff Member</th>"
    for day in FULL_DAYS:
        header += f"<th style='padding:8px 6px;text-align:center;font-size:11px;min-width:110px'>{day}</th>"
    header += "</tr>"

    rows = ""
    for s in staff:
        sid  = s["staff_id"]
        name = f"{s['first_name']} {s['last_name']}"
        tmpls = q("SELECT * FROM rota_templates WHERE staff_id=?", (sid,), fetch=True) or []
        tmpl_map = {dict(t)["day_of_week"]: dict(t) for t in tmpls}

        rows += f"<tr style='border-bottom:1px solid #f1f5f9'>"
        rows += f"<td style='padding:8px 12px;font-weight:700;font-size:13px'>{name}</td>"

        for dow in range(7):
            t = tmpl_map.get(dow, {})
            is_off = t.get("is_off", 1)
            start  = t.get("shift_start","") or ""
            end    = t.get("shift_end","")   or ""
            rows += f"""<td style='padding:3px'>
              <div style='display:flex;flex-direction:column;gap:2px'>
                <input type='time' form='tmpl_{sid}' name='start_{dow}'
                  value='{start}'
                  style='font-size:11px;padding:3px 5px;border:1px solid #e2e8f0;border-radius:6px;{"background:#f1f5f9" if is_off else ""}'>
                <input type='time' form='tmpl_{sid}' name='end_{dow}'
                  value='{end}'
                  style='font-size:11px;padding:3px 5px;border:1px solid #e2e8f0;border-radius:6px;{"background:#f1f5f9" if is_off else ""}'>
                <label style='display:flex;gap:4px;align-items:center;font-size:10px;
                              color:#64748b;text-transform:none;font-weight:400;margin:0'>
                  <input type='checkbox' form='tmpl_{sid}' name='off_{dow}'
                    {'checked' if is_off else ''} style='width:auto'> OFF
                </label>
              </div>
            </td>"""

        rows += f"<td style='padding:4px'>"
        rows += f"<form id='tmpl_{sid}' action='/rota/save-template' method='POST'>"
        rows += f"<input type='hidden' name='staff_id' value='{sid}'>"
        rows += f"<input type='hidden' name='store' value='{store}'>"
        rows += f"<button type='submit' class='btn-primary' style='padding:4px 10px;font-size:11px;white-space:nowrap'>&#128190; Save</button>"
        rows += f"</form></td>"
        rows += "</tr>"

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <a href='/rota?store={store}' style='color:#1e3a5f;font-size:13px;font-weight:700'>&#8592; Back to Rota</a>
        <div class='text-2xl font-black text-slate-800 mt-1'>&#9881;&#65039; Rota Templates — {store}</div>
        <div style='font-size:13px;color:#64748b;margin-top:2px'>
          Set each staff member's standard weekly pattern. New rotas start from these times.
        </div>
      </div>
      <div style='display:flex;gap:8px'>{store_btns}</div>
    </div>
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='overflow-x:auto'>
        <table style='width:100%;border-collapse:collapse;font-family:DM Sans,sans-serif'>
          <thead>{header}</thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>"""

    return page("Rota Templates", content, user, "rota")


@router.post("/rota/save-template")
async def save_template(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    form     = await request.form()
    staff_id = int(form.get("staff_id", 0))
    store    = form.get("store","")

    # Get store for staff
    s = q("SELECT store_name FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    store_name = dict(s[0])["store_name"] if s else store

    for dow in range(7):
        start  = form.get(f"start_{dow}","") or None
        end    = form.get(f"end_{dow}","")   or None
        is_off = 1 if form.get(f"off_{dow}") else 0
        hrs    = 0
        if start and end and not is_off:
            try:
                sh, sm = map(int, start.split(":"))
                eh, em = map(int, end.split(":"))
                hrs = round((eh*60+em - sh*60-sm)/60, 2)
            except: pass

        q("""INSERT INTO rota_templates
                (staff_id, store_name, day_of_week, shift_start, shift_end, hours, is_off)
             VALUES(?,?,?,?,?,?,?)
             ON CONFLICT(staff_id, day_of_week) DO UPDATE SET
                shift_start=excluded.shift_start,
                shift_end=excluded.shift_end,
                hours=excluded.hours,
                is_off=excluded.is_off""",
          (staff_id, store_name, dow, start, end, hrs, is_off))

    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/rota/templates?store={store}&msg={uq('Template saved')}",
        status_code=303)


ensure_rota_tables()
