"""timesheets routes."""
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
from haversine import haversine, Unit
from modules.rota import get_or_create_rota

router = APIRouter()


@router.get("/mobile-clock", response_class=HTMLResponse)
def mobile_clock_page(msg: str = "", msg_type: str = "success"):
    """Public GPS clock-in portal — no login required (uses staff name selection)."""
    # Get all active staff
    staff = q("SELECT staff_id, first_name, last_name, store_name FROM staff_profiles WHERE is_active=1 ORDER BY store_name, first_name",
              fetch=True) or []

    staff_opts = "<option value=''>-- Select your name --</option>"
    current_store = ""
    for s in staff:
        s = dict(s)
        if s["store_name"] != current_store:
            if current_store: staff_opts += "</optgroup>"
            staff_opts += "<optgroup label='" + s['store_name'] + "'>"
            current_store = s["store_name"]
        staff_opts += "<option value='" + str(s['staff_id']) + "'>" + s['first_name'] + " " + s['last_name'] + "</option>"
    if current_store: staff_opts += "</optgroup>"

    flash = ""
    if msg:
        col = "#dcfce7" if msg_type == "success" else "#fee2e2"
        tcol = "#166534" if msg_type == "success" else "#dc2626"
        flash = f"<div style='background:{col};color:{tcol};border-radius:10px;padding:12px 16px;font-size:14px;font-weight:700;margin-bottom:16px'>{msg}</div>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
  <title>Clock In — Snappy Snaps</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;700;900&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ font-family:'DM Sans',sans-serif; background:#0f2942; min-height:100vh;
            display:flex; align-items:center; justify-content:center; padding:20px; }}
    .card {{ background:white; border-radius:20px; padding:28px; width:100%; max-width:360px;
             box-shadow:0 20px 60px rgba(0,0,0,.3); }}
    h1 {{ font-size:22px; font-weight:900; color:#0f2942; margin-bottom:4px; }}
    .sub {{ font-size:12px; color:#94a3b8; font-weight:700; letter-spacing:.05em;
            text-transform:uppercase; margin-bottom:24px; }}
    label {{ font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase;
             letter-spacing:.05em; display:block; margin-bottom:4px; margin-top:14px; }}
    select {{ width:100%; border:1px solid #e2e8f0; border-radius:10px; padding:12px 14px;
              font-size:15px; font-family:'DM Sans',sans-serif; outline:none; appearance:none;
              background:white url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%2394a3b8' stroke-width='2' fill='none'/%3E%3C/svg%3E") no-repeat right 14px center; }}
    .btn {{ width:100%; padding:14px; border-radius:12px; font-size:16px; font-weight:900;
            border:none; cursor:pointer; font-family:'DM Sans',sans-serif; margin-top:10px;
            transition:all .15s; }}
    .btn-in  {{ background:#16a34a; color:white; }}
    .btn-in:hover  {{ background:#15803d; }}
    .btn-out {{ background:#dc2626; color:white; }}
    .btn-out:hover {{ background:#b91c1c; }}
    .lock {{ font-size:11px; color:#94a3b8; text-align:center; margin-top:14px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Staff Clock Portal</h1>
    <div class="sub">&#128274; GPS Verified &middot; Snappy Snaps</div>
    {flash}
    <form id="clockForm" action="/mobile-clock/submit" method="POST">
      <input type="hidden" name="latitude"  id="lat">
      <input type="hidden" name="longitude" id="lon">
      <input type="hidden" name="action"    id="action">
      <label>Your Name</label>
      <select name="staff_id" required>{staff_opts}</select>
      <button type="button" class="btn btn-in"  onclick="punch('clock_in')">&#128994; Clock In</button>
      <button type="button" class="btn btn-out" onclick="punch('clock_out')">&#128308; Clock Out</button>
    </form>
    <div class="lock">&#128205; Location required to verify attendance</div>
  </div>
  <script>
  function punch(action) {{
    const staff = document.querySelector('[name="staff_id"]').value;
    if (!staff) {{ alert('Please select your name first'); return; }}
    if (!navigator.geolocation) {{ alert('GPS not available on this device'); return; }}
    navigator.geolocation.getCurrentPosition(
      function(pos) {{
        document.getElementById('lat').value    = pos.coords.latitude;
        document.getElementById('lon').value    = pos.coords.longitude;
        document.getElementById('action').value = action;
        document.getElementById('clockForm').submit();
      }},
      function(err) {{ alert('Location access required. Please enable GPS and try again.'); }},
      {{ enableHighAccuracy:true, timeout:10000 }}
    );
  }}
  </script>
</body>
</html>""")


@router.post("/mobile-clock/submit", response_class=HTMLResponse)
async def submit_clock(request: Request):
    form      = await request.form()
    staff_id  = int(form.get("staff_id", 0))
    action    = form.get("action","clock_in")
    try:
        lat = float(form.get("latitude",  0))
        lon = float(form.get("longitude", 0))
    except:
        return HTMLResponse("<p>Invalid location data</p>", status_code=400)

    # Get staff details
    rows = q("SELECT * FROM staff_profiles WHERE staff_id=?", (staff_id,), fetch=True)
    if not rows:
        from urllib.parse import quote as uq
        return RedirectResponse(f"/mobile-clock?msg={uq('Staff member not found')}&msg_type=error", status_code=303)
    s          = dict(rows[0])
    store_name = s["store_name"]
    full_name  = f"{s['first_name']} {s['last_name']}"

    # GPS verification
    store_coords = STORE_GPS.get(store_name)
    if not store_coords:
        from urllib.parse import quote as uq
        return RedirectResponse(f"/mobile-clock?msg={uq('Store location not configured')}&msg_type=error", status_code=303)

    distance_m = haversine((lat, lon), store_coords, unit=Unit.METERS)
    on_site    = distance_m <= GEOFENCE_RADIUS_M

    if not on_site:
        from urllib.parse import quote as uq
        msg = f"Location rejected — you are {distance_m:.0f}m from {store_name} (max {GEOFENCE_RADIUS_M}m)"
        return RedirectResponse(f"/mobile-clock?msg={uq(msg)}&msg_type=error", status_code=303)

    # Record punch
    now_time = datetime.now().strftime("%H:%M:%S")
    now_date = datetime.now().strftime("%Y-%m-%d")

    if action == "clock_in":
        q("""INSERT INTO timesheets (staff_name, store_name, work_date, clock_in_time, status_flag)
             VALUES(?,?,?,?,'GPS_VERIFIED')
             ON CONFLICT(staff_name, store_name, work_date) DO UPDATE SET
                clock_in_time=excluded.clock_in_time, status_flag='GPS_VERIFIED'""",
          (full_name, store_name, now_date, now_time))
        msg = f"Clocked IN &#10003; — {full_name} at {store_name} {now_time}"
    else:
        q("""INSERT INTO timesheets (staff_name, store_name, work_date, clock_out_time, status_flag)
             VALUES(?,?,?,?,'GPS_VERIFIED')
             ON CONFLICT(staff_name, store_name, work_date) DO UPDATE SET
                clock_out_time=excluded.clock_out_time""",
          (full_name, store_name, now_date, now_time))
        msg = f"Clocked OUT &#10003; — {full_name} at {store_name} {now_time}"

    from urllib.parse import quote as uq
    return RedirectResponse(f"/mobile-clock?msg={uq(msg)}", status_code=303)


@router.get("/timesheets", response_class=HTMLResponse)
def timesheets_page(
    session:    str | None = Cookie(default=None),
    store:      str = "",
    month:      str = "",
    export:     str = ""
):
    redir, user = require_login(session)
    if redir: return redir

    if not month:
        month = datetime.now().strftime("%Y-%m")
    if not store and user.get("store_name"):
        store = user["store_name"]

    year, mon = map(int, month.split("-"))

    # Date range for this month
    from calendar import monthrange
    _, last_day = monthrange(year, mon)
    date_from   = f"{month}-01"
    date_to     = f"{month}-{last_day:02d}"

    # Get records
    conds  = ["work_date BETWEEN ? AND ?"]
    params = [date_from, date_to]
    if store:
        conds.append("store_name=?")
        params.append(store)
    if user["role"] == "staff":
        name = user.get("full_name","")
        if name:
            conds.append("staff_name=?")
            params.append(name)

    records = q(f"""SELECT * FROM timesheets WHERE {' AND '.join(conds)}
                    ORDER BY store_name, staff_name, work_date""",
                params, fetch=True) or []

    # CSV export
    if export == "csv":
        import csv, io
        buf = io.StringIO()
        w   = csv.writer(buf)
        w.writerow(["Staff Name","Store","Date","Clock In","Clock Out","Status","Hours Worked"])
        for r in records:
            r = dict(r)
            # Calculate hours
            hrs = ""
            if r.get("clock_in_time") and r.get("clock_out_time"):
                try:
                    ci = datetime.strptime(r["clock_in_time"],  "%H:%M:%S")
                    co = datetime.strptime(r["clock_out_time"], "%H:%M:%S")
                    hrs = f"{(co-ci).seconds/3600:.2f}"
                except: pass
            w.writerow([r["staff_name"],r["store_name"],r["work_date"],
                        r.get("clock_in_time",""),r.get("clock_out_time",""),
                        r.get("status_flag",""),hrs])
        from fastapi.responses import Response
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment;filename=timesheets_{month}_{store}.csv"})

    # Month navigation
    prev_d = (datetime(year, mon, 1) - timedelta(days=1))
    next_d = (datetime(year, mon, last_day) + timedelta(days=1))
    prev_m = prev_d.strftime("%Y-%m")
    next_m = next_d.strftime("%Y-%m")

    # Store filter
    store_btns = ""
    if user["role"] in ("owner","manager"):
        for sv, sl in [("","Both"),("Uxbridge","Uxbridge"),("Newbury","Newbury")]:
            cls = "btn-primary" if store==sv else "btn-secondary"
            store_btns += f"<a href='/timesheets?store={sv}&month={month}' class='{cls}' style='padding:5px 14px;font-size:13px'>{sl}</a>"

    # Build table
    rows_html = ""
    for r in records:
        r   = dict(r)
        hrs = ""
        if r.get("clock_in_time") and r.get("clock_out_time"):
            try:
                ci  = datetime.strptime(r["clock_in_time"],  "%H:%M:%S")
                co  = datetime.strptime(r["clock_out_time"], "%H:%M:%S")
                hrs = f"{(co-ci).seconds/3600:.2f}h"
            except: pass
        status_cls = "badge-paid" if r.get("status_flag")=="GPS_VERIFIED" else "badge-unpaid"
        out_val    = r.get("clock_out_time") or "<span style='color:#d97706'>On shift</span>"
        rows_html += f"""<tr>
          <td style='font-weight:700'>{r['staff_name']}</td>
          <td style='font-size:12px;color:#64748b'>{r['store_name']}</td>
          <td class='mono' style='font-size:12px'>{r['work_date']}</td>
          <td class='mono' style='color:#16a34a;font-weight:700'>{r.get('clock_in_time') or '—'}</td>
          <td class='mono' style='color:#dc2626;font-weight:700'>{out_val}</td>
          <td class='mono' style='font-weight:700'>{hrs}</td>
          <td><span class='{status_cls}'>{r.get("status_flag") or "—"}</span></td>
        </tr>"""

    month_label = datetime(year, mon, 1).strftime("%B %Y")

    content = f"""
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div class='text-2xl font-black text-slate-800'>&#9200; Timesheets — {month_label}</div>
      <div style='display:flex;gap:8px;flex-wrap:wrap;align-items:center'>
        {store_btns}
        <a href='/timesheets?store={store}&month={prev_m}' class='btn-secondary' style='padding:5px 12px'>&#8592;</a>
        <a href='/timesheets?store={store}&month={next_m}' class='btn-secondary' style='padding:5px 12px'>&#8594;</a>
        <a href='/timesheets?store={store}&month={month}&export=csv'
           class='btn-primary' style='padding:6px 16px;font-size:13px'>
          &#11015;&#65039; Export CSV for Payroll
        </a>
      </div>
    </div>
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead>
            <tr>
              <th>Staff Member</th><th>Store</th><th>Date</th>
              <th>Clock In</th><th>Clock Out</th><th>Hours</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows_html or '<tr><td colspan="7" style="text-align:center;padding:32px;color:#94a3b8">No records for this period</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>"""

    return page("Timesheets", content, user, "timesheets")


@router.get("/rota/pdf")
def rota_pdf(
    store:      str = "",
    week_start: str = "",
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from fastapi.responses import Response
    import io

    week_dates = get_week_dates(week_start)
    week_end   = week_dates[-1]
    rota       = get_or_create_rota(store, week_start)
    shifts     = {(s["staff_id"], s["shift_date"]): s for s in rota.get("shifts", [])}
    staff      = q("SELECT * FROM staff_profiles WHERE store_name=? AND is_active=1 ORDER BY first_name",
                   (store,), fetch=True) or []

    # Colours
    COL_NAVY   = colors.HexColor("#0f2942")
    COL_BLUE   = colors.HexColor("#1e3a5f")
    COL_GREEN  = colors.HexColor("#dcfce7")
    COL_GREEN2 = colors.HexColor("#166534")
    COL_RED    = colors.HexColor("#fee2e2")
    COL_RED2   = colors.HexColor("#dc2626")
    COL_AMBER  = colors.HexColor("#fef3c7")
    COL_AMBER2 = colors.HexColor("#92400e")
    COL_LGREY  = colors.HexColor("#f8fafc")
    COL_GREY   = colors.HexColor("#e2e8f0")
    COL_WHITE  = colors.white

    buf  = io.BytesIO()
    doc  = SimpleDocTemplate(buf, pagesize=landscape(A4),
                             leftMargin=10*mm, rightMargin=10*mm,
                             topMargin=12*mm, bottomMargin=12*mm)

    styles  = getSampleStyleSheet()
    title_s = ParagraphStyle("title", fontSize=16, fontName="Helvetica-Bold",
                              textColor=COL_NAVY, alignment=TA_LEFT)
    sub_s   = ParagraphStyle("sub", fontSize=9, fontName="Helvetica",
                              textColor=colors.HexColor("#64748b"), alignment=TA_LEFT)
    cell_s  = ParagraphStyle("cell", fontSize=8, fontName="Helvetica-Bold",
                              alignment=TA_CENTER, leading=10)
    small_s = ParagraphStyle("small", fontSize=7, fontName="Helvetica",
                              alignment=TA_CENTER, textColor=colors.HexColor("#64748b"), leading=8)
    name_s  = ParagraphStyle("name", fontSize=9, fontName="Helvetica-Bold",
                              alignment=TA_LEFT, textColor=COL_NAVY)
    hdr_s   = ParagraphStyle("hdr", fontSize=8, fontName="Helvetica-Bold",
                              alignment=TA_CENTER, textColor=COL_WHITE)

    week_label = f"{datetime.strptime(week_start,'%Y-%m-%d').strftime('%d %b')} – {datetime.strptime(week_end,'%Y-%m-%d').strftime('%d %b %Y')}"
    status     = rota.get("status","draft").upper()

    story = [
        Paragraph(f"Snappy Snaps {store} — Weekly Rota", title_s),
        Spacer(1, 3*mm),
        Paragraph(f"Week: {week_label}  ·  Status: {status}  ·  Generated: {datetime.now().strftime('%d %b %Y %H:%M')}", sub_s),
        Spacer(1, 5*mm),
    ]

    # Build table data
    # Header row
    header = [Paragraph("Staff Member", hdr_s)]
    for i, date_str in enumerate(week_dates):
        d   = datetime.strptime(date_str, "%Y-%m-%d")
        txt = f"{DAYS[i]}\n{d.strftime('%d %b')}"
        header.append(Paragraph(txt, hdr_s))
    header.append(Paragraph("Hrs", hdr_s))

    table_data  = [header]
    table_style = [
        ("BACKGROUND", (0,0), (-1,0), COL_NAVY),
        ("TEXTCOLOR",  (0,0), (-1,0), COL_WHITE),
        ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0), (-1,0), 8),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("GRID",       (0,0), (-1,-1), 0.3, COL_GREY),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [COL_WHITE, COL_LGREY]),
        ("LEFTPADDING",  (0,0), (-1,-1), 4),
        ("RIGHTPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
    ]

    # Day totals tracking
    day_hrs   = [0.0] * 7
    day_count = [0]   * 7
    week_hrs  = 0.0

    for row_idx, s in enumerate(staff):
        sid   = s["staff_id"]
        name  = f"{s['first_name']} {s['last_name']}"
        row   = [Paragraph(name, name_s)]
        total = 0.0
        r     = row_idx + 1

        for i, date_str in enumerate(week_dates):
            sh      = shifts.get((sid, date_str), {})
            is_off  = sh.get("is_off", 1)
            absence = sh.get("absence_type")
            start   = sh.get("shift_start") or ""
            end     = sh.get("shift_end")   or ""
            hrs     = sh.get("hours") or 0

            if absence:
                labels = {"H":"Holiday","S":"Sick","B":"Bank Hol","AL":"Auth Leave","L":"Late"}
                lbl    = labels.get(absence, absence)
                cell   = Paragraph(lbl, ParagraphStyle("ab", fontSize=7, fontName="Helvetica-Bold",
                                   alignment=TA_CENTER, textColor=COL_GREEN2))
                table_style.append(("BACKGROUND", (i+1, r), (i+1, r), COL_GREEN))
                if absence == "S":
                    table_style.append(("BACKGROUND", (i+1, r), (i+1, r), COL_RED))
                    cell = Paragraph(lbl, ParagraphStyle("ab", fontSize=7, fontName="Helvetica-Bold",
                                     alignment=TA_CENTER, textColor=COL_RED2))
            elif is_off:
                cell = Paragraph("OFF", ParagraphStyle("off", fontSize=7, fontName="Helvetica",
                                 alignment=TA_CENTER, textColor=colors.HexColor("#cbd5e1")))
            else:
                total += hrs
                day_hrs[i]   += hrs
                day_count[i] += 1
                week_hrs     += hrs
                shift_txt = f"{start}–{end}\n{hrs:.1f}h"
                cell = Paragraph(shift_txt, ParagraphStyle("sh", fontSize=8, fontName="Helvetica-Bold",
                                 alignment=TA_CENTER, textColor=COL_BLUE, leading=10))
                table_style.append(("BACKGROUND", (i+1, r), (i+1, r), colors.HexColor("#eff6ff")))

            row.append(cell)

        hrs_cell = Paragraph(f"{total:.1f}", ParagraphStyle("hrs", fontSize=9,
                             fontName="Helvetica-Bold", alignment=TA_CENTER,
                             textColor=COL_NAVY if total else colors.HexColor("#cbd5e1")))
        row.append(hrs_cell)
        table_data.append(row)

    # Totals row
    totals_row = [Paragraph("TOTALS", ParagraphStyle("tot", fontSize=8, fontName="Helvetica-Bold",
                             alignment=TA_LEFT, textColor=COL_NAVY))]
    for i in range(7):
        txt = f"{day_hrs[i]:.1f}h\n{day_count[i]} staff"
        totals_row.append(Paragraph(txt, ParagraphStyle("dt", fontSize=7, fontName="Helvetica-Bold",
                                    alignment=TA_CENTER, textColor=COL_NAVY, leading=9)))
    totals_row.append(Paragraph(f"{week_hrs:.1f}", ParagraphStyle("wt", fontSize=10,
                                fontName="Helvetica-Bold", alignment=TA_CENTER, textColor=COL_NAVY)))
    table_data.append(totals_row)

    n = len(table_data)
    table_style.append(("BACKGROUND",  (0, n-1), (-1, n-1), COL_LGREY))
    table_style.append(("FONTNAME",    (0, n-1), (-1, n-1), "Helvetica-Bold"))
    table_style.append(("LINEABOVE",   (0, n-1), (-1, n-1), 1.5, COL_GREY))

    # Column widths — name col wider, day cols equal, hrs col narrow
    page_w    = landscape(A4)[0] - 20*mm
    name_w    = 38*mm
    hrs_w     = 14*mm
    day_w     = (page_w - name_w - hrs_w) / 7
    col_widths = [name_w] + [day_w]*7 + [hrs_w]
    row_height = 14*mm

    tbl = Table(table_data, colWidths=col_widths, rowHeights=row_height)
    tbl.setStyle(TableStyle(table_style))
    story.append(tbl)

    # Legend
    story.append(Spacer(1, 4*mm))
    legend_data = [[
        Paragraph("Legend:", ParagraphStyle("lg", fontSize=7, fontName="Helvetica-Bold")),
        Paragraph("■ Working shift", ParagraphStyle("lg2", fontSize=7, fontName="Helvetica",
                  textColor=COL_BLUE)),
        Paragraph("■ Holiday", ParagraphStyle("lg3", fontSize=7, fontName="Helvetica",
                  textColor=COL_GREEN2)),
        Paragraph("■ Sick", ParagraphStyle("lg4", fontSize=7, fontName="Helvetica",
                  textColor=COL_RED2)),
        Paragraph("Hours shown are PAID hours (30 min break deducted for shifts ≥ 4h)",
                  ParagraphStyle("lg5", fontSize=7, fontName="Helvetica",
                  textColor=colors.HexColor("#64748b"))),
    ]]
    legend = Table(legend_data, colWidths=[20*mm, 28*mm, 20*mm, 15*mm, 120*mm])
    legend.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE")]))
    story.append(legend)

    doc.build(story)
    buf.seek(0)

    filename = f"Rota_{store}_{week_start}.pdf"
    return Response(content=buf.read(), media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})
