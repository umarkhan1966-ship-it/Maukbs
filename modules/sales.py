"""sales routes."""
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


def ensure_sales_tables():
    conn = db()
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_cashsheet (
            entry_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name     TEXT NOT NULL,
            sale_date      TEXT NOT NULL,
            z_read_no      INTEGER,
            -- 22 Sales categories
            digital_printing   REAL DEFAULT 0,
            other_dp           REAL DEFAULT 0,
            instant_prints     REAL DEFAULT 0,
            reprint_enlarge    REAL DEFAULT 0,
            internet_orders    REAL DEFAULT 0,
            passport           REAL DEFAULT 0,
            film_media         REAL DEFAULT 0,
            graphic_design     REAL DEFAULT 0,
            large_format       REAL DEFAULT 0,
            toner_laser        REAL DEFAULT 0,
            batteries          REAL DEFAULT 0,
            frames_albums      REAL DEFAULT 0,
            photogifts         REAL DEFAULT 0,
            backup_media       REAL DEFAULT 0,
            dvd_transfer       REAL DEFAULT 0,
            studio             REAL DEFAULT 0,
            sundry             REAL DEFAULT 0,
            promotions         REAL DEFAULT 0,
            rcs_std_vat        REAL DEFAULT 0,
            rcs_zero           REAL DEFAULT 0,
            photobooks         REAL DEFAULT 0,
            type_b_sales       REAL DEFAULT 0,
            discount_amount    REAL DEFAULT 0,
            -- Card breakdown
            card_visa          REAL DEFAULT 0,
            card_visa_debit    REAL DEFAULT 0,
            card_mastercard    REAL DEFAULT 0,
            card_mc_debit      REAL DEFAULT 0,
            card_maestro_dom   REAL DEFAULT 0,
            card_maestro_int   REAL DEFAULT 0,
            card_solo          REAL DEFAULT 0,
            card_electron      REAL DEFAULT 0,
            card_amex          REAL DEFAULT 0,
            card_discover      REAL DEFAULT 0,
            card_other         REAL DEFAULT 0,
            -- Cash
            cash_taken         REAL DEFAULT 0,
            opening_cash_bf    REAL DEFAULT 0,
            -- Paid outs
            paid_out_total     REAL DEFAULT 0,
            paid_out_notes     TEXT,
            -- Till reconciliation
            till_credit_sales  REAL DEFAULT 0,
            till_internet_sales REAL DEFAULT 0,
            total_cash_store   REAL DEFAULT 0,
            -- Meta
            entered_by         TEXT,
            submitted_at       TEXT DEFAULT (datetime('now')),
            is_locked          INTEGER DEFAULT 0,
            notes              TEXT,
            UNIQUE(store_name, sale_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS paid_outs (
            paidout_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name     TEXT NOT NULL,
            entry_date     TEXT NOT NULL,
            description    TEXT NOT NULL,
            amount         REAL NOT NULL,
            category       TEXT,
            entered_by     TEXT,
            created_at     TEXT DEFAULT (datetime('now'))
        )
    """)
    # Note: sales_targets is defined authoritatively in init_db() (with the
    # target_pct column) — it is intentionally not redefined here.
    conn.commit()
    conn.close()


SALES_CATEGORIES = [
    ("digital_printing",  "1",  "Digital Printing",    "trans_digital_printing"),
    ("other_dp",          "2",  "Other D&P",           "trans_other_dp"),
    ("instant_prints",    "3",  "Instant Prints",      "trans_instant_prints"),
    ("reprint_enlarge",   "4",  "Reprint/Enlarge",     "trans_reprint_enlarge"),
    ("internet_orders",   "5",  "Internet Orders",     "trans_internet_orders"),
    ("passport",          "6",  "Passport",            "trans_passport"),
    ("film_media",        "7",  "Film Media",          "trans_film_media"),
    ("graphic_design",    "8",  "Graphic Design",      "trans_graphic_design"),
    ("large_format",      "9",  "Large Format",        "trans_large_format"),
    ("toner_laser",       "10", "Toner/Laser Output",  "trans_toner_laser"),
    ("batteries",         "11", "Batteries",           "trans_batteries"),
    ("frames_albums",     "12", "Frames & Albums",     "trans_frames_albums"),
    ("photogifts",        "13", "Photogifts",          "trans_photogifts"),
    ("backup_media",      "14", "Backup to Media",     "trans_backup_media"),
    ("dvd_transfer",      "15", "DVD Transfer",        "trans_dvd_transfer"),
    ("studio",            "16", "Studio",              "trans_studio"),
    ("sundry",            "17", "Sundry",              "trans_sundry"),
    ("promotions",        "18", "Promotions",          "trans_promotions"),
    ("rcs_std_vat",       "19", "RCS (STD VAT)",       "trans_rcs_std_vat"),
    ("rcs_zero",          "20", "RCS (ZERO)",          "trans_rcs_zero"),
    ("photobooks",        "21", "Photobooks",          "trans_photobooks"),
    ("type_b_sales",      "22", "TYPE B Sales",        "trans_type_b_sales"),
]


CARD_TYPES = [
    ("card_visa",        "VISA"),
    ("card_visa_debit",  "VISA DEBIT"),
    ("card_mastercard",  "MASTERCARD"),
    ("card_mc_debit",    "MASTERCARD DEBIT"),
    ("card_maestro_dom", "MAESTRO DOM"),
    ("card_maestro_int", "MAESTRO INT"),
    ("card_solo",        "SOLO"),
    ("card_electron",    "ELECTRON"),
    ("card_amex",        "AMEX"),
    ("card_discover",    "DISCOVER"),
    ("card_other",       "OTHER"),
]


@router.get("/sales", response_class=HTMLResponse)
def sales_page(
    session:    str | None = Cookie(default=None),
    store:      str = "",
    week_start: str = "",
    msg:        str = ""
):
    redir, user = require_login(session)
    if redir: return redir

    if not store and user.get("store_name"):
        store = user["store_name"]
    if not store:
        store = "Uxbridge"
    if not week_start:
        week_start = get_week_start()

    week_dates = get_week_dates(week_start)
    week_end   = week_dates[-1]
    prev_week  = (datetime.strptime(week_start, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week  = (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
    flash      = f"<div class='flash-success'>{msg}</div>" if msg else ""
    is_mgr     = user["role"] in ("owner","manager")

    # Get all cashsheet entries for this week
    entries = q("""SELECT * FROM daily_cashsheet
                   WHERE store_name=? AND sale_date BETWEEN ? AND ?
                   ORDER BY sale_date""",
                (store, week_dates[0], week_dates[-1]), fetch=True) or []
    entry_map = {dict(e)["sale_date"]: dict(e) for e in entries}

    # Store switcher
    store_btns = ""
    if user["role"] in ("owner","manager"):
        for sv in ["Uxbridge","Newbury"]:
            cls = "btn-primary" if sv == store else "btn-secondary"
            store_btns += f"<a href='/sales?store={sv}&week_start={week_start}' class='{cls}' style='padding:5px 14px;font-size:13px'>{sv}</a>"

    # Week summary cards
    week_total = sum(
        sum((e.get(col, 0) or 0) for col, _, _, _ in SALES_CATEGORIES) + (e.get("discount_amount", 0) or 0)
        for e in [entry_map.get(d, {}) for d in week_dates]
    )
    days_entered = sum(1 for d in week_dates if d in entry_map)

    # Get this month's target
    today     = datetime.now()
    target_row = q("SELECT target_amount FROM sales_targets WHERE store_name=? AND year=? AND month=?",
                   (store, today.year, today.month), fetch=True)
    monthly_target = dict(target_row[0])["target_amount"] if target_row else 0

    # Month to date
    month_start = today.strftime("%Y-%m-01")
    month_end   = today.strftime("%Y-%m-%d")
    mtd_rows    = q("""SELECT * FROM daily_cashsheet
                       WHERE store_name=? AND sale_date BETWEEN ? AND ?""",
                    (store, month_start, month_end), fetch=True) or []
    mtd_total   = sum(
        sum((dict(e).get(col, 0) or 0) for col, _, _, _ in SALES_CATEGORIES) + (dict(e).get("discount_amount", 0) or 0)
        for e in mtd_rows
    )
    target_pct  = (mtd_total / monthly_target * 100) if monthly_target else 0
    target_col  = "#16a34a" if target_pct >= 100 else ("#d97706" if target_pct >= 75 else "#dc2626")

    summary_cards = f"""
    <div class='grid gap-4' style='grid-template-columns:repeat(auto-fit,minmax(160px,1fr))'>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>This Week</div>
        <div style='font-size:24px;font-weight:900;color:#0f2942'>£{week_total:,.2f}</div>
        <div style='font-size:11px;color:#94a3b8'>{days_entered}/7 days entered</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Month to Date</div>
        <div style='font-size:24px;font-weight:900;color:#0f2942'>£{mtd_total:,.2f}</div>
        <div style='font-size:11px;color:#94a3b8'>{today.strftime("%B %Y")}</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Monthly Target</div>
        <div style='font-size:24px;font-weight:900;color:{target_col}'>
          {'£'+f"{monthly_target:,.0f}" if monthly_target else "Not set"}
        </div>
        <div style='font-size:11px;color:{target_col}'>{target_pct:.0f}% of target</div>
      </div>
      <div class='card py-3 text-center'>
        <div style='font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase'>Remaining</div>
        <div style='font-size:24px;font-weight:900;color:{"#16a34a" if monthly_target-mtd_total<=0 else "#dc2626"}'>
          £{max(0,monthly_target-mtd_total):,.2f}
        </div>
        <div style='font-size:11px;color:#94a3b8'>to reach target</div>
      </div>
    </div>"""

    # Weekly grid
    header = "<tr style='background:#0f2942;color:white'>"
    header += "<th style='padding:10px 12px;text-align:left;font-size:12px;min-width:180px'>Category</th>"
    for i, date_str in enumerate(week_dates):
        d      = datetime.strptime(date_str, "%Y-%m-%d")
        is_today = date_str == today.strftime("%Y-%m-%d")
        has_data = date_str in entry_map
        bg = "background:#1e3a5f" if is_today else ""
        tick = " ✅" if has_data else ""
        header += f"<th style='padding:8px 6px;text-align:right;font-size:11px;min-width:90px;{bg}'>{DAYS[i]}<br><span style='font-size:10px;opacity:.7'>{d.strftime('%d %b')}{tick}</span></th>"
    header += "<th style='padding:8px;text-align:right;font-size:11px'>Week Total</th></tr>"

    rows_html = ""
    cat_totals = {col: 0 for col, _, _, _ in SALES_CATEGORIES}

    for col, num, label, trans_col in SALES_CATEGORIES:
        row_total = sum((entry_map.get(d, {}).get(col, 0) or 0) for d in week_dates)
        cat_totals[col] = row_total
        cells = ""
        for date_str in week_dates:
            val = entry_map.get(date_str, {}).get(col, 0) or 0
            cells += f"<td style='padding:6px 8px;text-align:right;font-size:12px'>{'£'+f'{val:.2f}' if val else '—'}</td>"
        rows_html += f"""<tr style='border-bottom:1px solid #f1f5f9'>
          <td style='padding:6px 12px;font-size:13px;color:#334155'><span style='color:#94a3b8;font-size:11px'>{num}</span> {label}</td>
          {cells}
          <td style='padding:6px 8px;text-align:right;font-size:13px;font-weight:700;color:#0f2942'>{'£'+f'{row_total:.2f}' if row_total else '—'}</td>
        </tr>"""

    # Discount row
    disc_row_total = sum((entry_map.get(d, {}).get("discount_amount", 0) or 0) for d in week_dates)
    disc_cells = ""
    for d in week_dates:
        dval = entry_map.get(d, {}).get('discount_amount', 0) or 0
        disc_cells += "<td style='padding:6px 8px;text-align:right;font-size:12px;color:#dc2626'>" + ('£' + f'{dval:.2f}' if dval else '—') + "</td>"
    rows_html += f"""<tr style='border-bottom:2px solid #e2e8f0;background:#fff5f5'>
      <td style='padding:6px 12px;font-size:13px;color:#dc2626;font-weight:700'>Less: Discounts</td>
      {disc_cells}
      <td style='padding:6px 8px;text-align:right;font-size:13px;font-weight:700;color:#dc2626'>{'£'+f'{disc_row_total:.2f}' if disc_row_total else '—'}</td>
    </tr>"""

    # Total row
    day_totals = []
    for date_str in week_dates:
        e   = entry_map.get(date_str, {})
        tot = sum((e.get(col, 0) or 0) for col, _, _, _ in SALES_CATEGORIES) + (e.get("discount_amount", 0) or 0)
        day_totals.append(f"<td style='padding:8px;text-align:right;font-size:13px;font-weight:900;color:#0f2942;background:#f8fafc'>{'£'+f'{tot:,.2f}' if tot else '—'}</td>")

    rows_html += f"""<tr style='background:#f8fafc;border-top:2px solid #e2e8f0'>
      <td style='padding:8px 12px;font-size:13px;font-weight:900;color:#0f2942'>TOTAL SALES</td>
      {"".join(day_totals)}
      <td style='padding:8px;text-align:right;font-size:14px;font-weight:900;color:#0f2942'>£{week_total:,.2f}</td>
    </tr>"""

    # Action buttons
    action_btns = f"""
    <div style='display:flex;gap:8px;flex-wrap:wrap'>
      <a href='/sales/enter?store={store}&date={today.strftime("%Y-%m-%d")}' class='btn-primary'>
        &#128221; Enter Today's Sales
      </a>
      {'<a href="/sales/targets?store=' + store + '" class="btn-secondary">&#127919; Manage Targets</a>' if is_mgr else ''}
      <a href='/sales/franchise-return?store={store}&week_start={week_start}' class='btn-secondary'>
        &#128196; Franchise Return
      </a>
      <a href='/sales/managers-report?store={store}&week_start={week_start}' class='btn-secondary'>
        &#128200; Manager's Report
      </a>
    </div>"""

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <div class='text-2xl font-black text-slate-800'>&#128200; Sales — {store}</div>
        <div style='font-size:13px;color:#64748b;margin-top:2px'>
          Week: {datetime.strptime(week_start,"%Y-%m-%d").strftime("%d %b")} –
          {datetime.strptime(week_end,"%Y-%m-%d").strftime("%d %b %Y")}
        </div>
      </div>
      <div style='display:flex;gap:8px;flex-wrap:wrap;align-items:center'>
        {store_btns}
        <a href='/sales?store={store}&week_start={prev_week}' class='btn-secondary' style='padding:5px 12px'>&#8592;</a>
        <a href='/sales?store={store}' class='btn-secondary' style='padding:5px 12px'>This Week</a>
        <a href='/sales?store={store}&week_start={next_week}' class='btn-secondary' style='padding:5px 12px'>&#8594;</a>
      </div>
    </div>
    {summary_cards}
    {action_btns}
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='overflow-x:auto'>
        <table style='width:100%;border-collapse:collapse;font-family:DM Sans,sans-serif'>
          <thead>{header}</thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </div>"""

    return page("Sales", content, user, "sales")


@router.get("/sales/enter", response_class=HTMLResponse)
def sales_entry_form(
    store:   str = "",
    date:    str = "",
    session: str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if not store and user.get("store_name"):
        store = user["store_name"]
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    d_fmt    = datetime.strptime(date, "%Y-%m-%d").strftime("%A %d %B %Y")
    is_thurs = datetime.strptime(date, "%Y-%m-%d").weekday() == 3

    # Get existing entry
    existing = q("SELECT * FROM daily_cashsheet WHERE store_name=? AND sale_date=?",
                 (store, date), fetch=True)
    e = dict(existing[0]) if existing else {}

    # Get previous day for B/F
    prev_date  = (datetime.strptime(date,"%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    prev_entry = q("SELECT * FROM daily_cashsheet WHERE store_name=? AND sale_date=?",
                   (store, prev_date), fetch=True)
    prev_e     = dict(prev_entry[0]) if prev_entry else {}
    bf_auto    = e.get("opening_cash_bf") or prev_e.get("actual_cash_cf") or 0
    internet_orders_val = e.get("internet_orders") or 0
    prev_z     = prev_e.get("z_read_no") or 0
    prev_z2    = prev_e.get("z2_read_no") or 0

    def fv(col):
        v = e.get(col, 0)
        return ("%.2f" % v) if isinstance(v,(int,float)) and v else ""
    def fvs(col): return str(e.get(col,"") or "")
    def fvi(col): return str(int(e.get(col,0) or 0)) if e.get(col) else ""
    def fvn(col): return str(int(e.get(col,0) or 0)) if e.get(col) else ""

    prev_d = (datetime.strptime(date,"%Y-%m-%d")-timedelta(days=1)).strftime("%Y-%m-%d")
    next_d = (datetime.strptime(date,"%Y-%m-%d")+timedelta(days=1)).strftime("%Y-%m-%d")
    z_cur  = fvi("z_read_no")
    z2_cur = fvi("z2_read_no")

    # Build sales category rows
    cat_rows = ""
    for col, num, label, trans_col in SALES_CATEGORIES:
        v    = fv(col)
        tcnt = fvi(trans_col)
        act  = e.get(col,0) or 0
        tnum = e.get(trans_col,0) or 0
        vpt  = ("%.2f" % (act/tnum)) if tnum else "&mdash;"
        cat_rows += (
            "<tr style='border-bottom:1px solid #f1f5f9'>"
            "<td style='padding:3px 8px;font-size:11px;color:#334155;white-space:nowrap'>"
            "<span style='color:#94a3b8;font-size:10px'>" + num + "</span> - " + label + "</td>"
            "<td style='padding:2px 3px;width:55px'>"
            "<input type='number' name='" + trans_col + "' value='" + tcnt + "'"
            " min='0' step='1' oninput='updVPT(\"" + col + "\",\"" + trans_col + "\")'"
            " onblur='if(this.value)this.value=Math.round(parseFloat(this.value)||0)'"
            " placeholder='0'"
            " style='width:100%;text-align:right;border:1px solid #d1d5db;border-radius:4px;"
            "padding:3px 4px;font-size:12px;background:#fefce8'>"
            "</td>"
            "<td style='padding:3px 5px;width:65px;text-align:right;font-size:11px;"
            "font-family:DM Mono,monospace;background:#f0fdf4;color:#166534'"
            " id='vpt_" + col + "'>" + vpt + "</td>"
            "<td style='padding:2px 3px;width:75px'>"
            "<input type='number' step='0.01' name='" + col + "' value='" + v + "'"
            " onblur='if(this.value&&!isNaN(this.value))this.value=parseFloat(this.value).toFixed(2)'"
            " oninput='updTot();updVPT(\"" + col + "\",\"" + trans_col + "\")'"
            " placeholder='0.00'"
            " style='width:100%;text-align:right;border:1px solid #d1d5db;border-radius:4px;"
            "padding:3px 4px;font-size:12px;background:#fefce8;font-family:DM Mono,monospace'>"
            "</td></tr>"
        )

    # Blank line 24
    cat_rows += (
        "<tr style='border-bottom:1px solid #f1f5f9'>"
        "<td style='padding:3px 8px;font-size:11px;color:#94a3b8'><span style='font-size:10px'>24</span> - </td>"
        "<td style='padding:2px 3px'><input type='number' min='0' step='1' placeholder='0'"
        " style='width:100%;text-align:right;border:1px solid #d1d5db;border-radius:4px;padding:3px 4px;font-size:12px;background:#fefce8'></td>"
        "<td style='background:#f0fdf4'>&mdash;</td>"
        "<td style='padding:2px 3px'><input type='number' step='0.01' placeholder='0.00'"
        " style='width:100%;text-align:right;border:1px solid #d1d5db;border-radius:4px;padding:3px 4px;font-size:12px;background:#fefce8;font-family:DM Mono,monospace'></td>"
        "</tr>"
    )
    # Discount line 25
    disc_amt = fv("discount_amount") or ""
    cat_rows += (
        "<tr style='background:#fff5f5;border-top:1px solid #fca5a5'>"
        "<td style='padding:3px 8px;font-size:11px;font-weight:700;color:#dc2626;white-space:nowrap'>"
        "<span style='font-size:10px;color:#dc2626'>25</span> - % - Discount (Enter as -ve)</td>"
        "<td style='padding:2px 3px'><input type='number' name='discount_trans' min='0' step='1' placeholder='0'"
        " oninput='updDiscVPT()'"
        " style='width:100%;text-align:right;border:1px solid #fca5a5;border-radius:4px;padding:3px 4px;font-size:12px;background:#fef2f2'></td>"
        "<td id='vpt_discount' style='background:#f0fdf4;text-align:right;font-size:11px;font-family:DM Mono,monospace;color:#166534'>&mdash;</td>"
        "<td style='padding:2px 3px'><input type='number' step='0.01' name='discount_amount' value='" + disc_amt + "'"
        " onfocus='highlightRow(this)' onblur='if(this.value&&!isNaN(this.value))this.value=parseFloat(this.value).toFixed(2)'"
        " oninput='updTot();updDiscVPT()' placeholder='e.g. -5.00'"
        " style='width:100%;text-align:right;border:1px solid #fca5a5;border-radius:4px;padding:3px 4px;font-size:12px;color:#dc2626;background:#fef2f2;font-family:DM Mono,monospace'></td>"
        "</tr>"
    )

    # Card rows
    card_rows = ""
    # Internet Orders auto-populated row first
    io_val = fv("internet_orders") or ""
    card_rows += (
        "<tr style='border-bottom:1px solid #f1f5f9;background:#f0f9ff'>"
        "<td style='padding:4px 10px;font-size:12px;color:#334155'>Internet Orders <span style='font-size:10px;color:#94a3b8'>(auto)</span></td>"
        "<td style='padding:2px 6px'>"
        "<input type='number' step='0.01' name='card_internet_orders' id='card_io_field'"
        " value='" + io_val + "' readonly"
        " style='width:100%;text-align:right;border:1px solid #bae6fd;border-radius:5px;"
        "padding:4px 6px;font-size:13px;font-family:DM Mono,monospace;background:#f0f9ff'>"
        "</td></tr>"
    )
    for col, label in CARD_TYPES:
        card_rows += (
            "<tr style='border-bottom:1px solid #f1f5f9'>"
            "<td style='padding:4px 10px;font-size:12px;color:#334155'>" + label + "</td>"
            "<td style='padding:2px 6px'>"
            "<input type='number' step='0.01' name='" + col + "' value='" + fv(col) + "'"
            " onblur='if(this.value)this.value=(parseFloat(this.value)||0).toFixed(2)'"
            " oninput='updCards()' placeholder='0.00'"
            " style='width:100%;text-align:right;border:1px solid #e2e8f0;border-radius:5px;"
            "padding:4px 6px;font-size:13px;font-family:DM Mono,monospace'>"
            "</td></tr>"
        )

    # Denomination rows
    denoms = [
        ("notes_50",50.00,"£50"),("notes_20",20.00,"£20"),("notes_10",10.00,"£10"),
        ("notes_5",5.00,"£5"),("coins_2",2.00,"£2"),("coins_1",1.00,"£1"),
        ("coins_50p",0.50,"50p"),("coins_20p",0.20,"20p"),("coins_10p",0.10,"10p"),
        ("coins_5p",0.05,"5p"),("coins_2p",0.02,"2p"),("coins_1p",0.01,"1p"),
    ]
    denom_rows = ""
    for col, val, label in denoms:
        cnt = fvn(col)
        dv  = ("£" + "%.2f" % (int(e.get(col,0) or 0)*val)) if e.get(col) else "&mdash;"
        denom_rows += (
            "<tr style='border-bottom:1px solid #f1f5f9'>"
            "<td style='padding:3px 8px;font-size:12px;font-weight:700;color:#334155'>" + label + "</td>"
            "<td style='padding:2px 4px'>"
            "<input type='number' name='" + col + "' value='" + cnt + "'"
            " onfocus='highlightRow(this)' oninput='updDenoms()' placeholder='0' min='0' step='1'"
            " style='width:60px;text-align:right;border:1px solid #e2e8f0;border-radius:5px;padding:3px 5px;font-size:12px'>"
            "</td>"
            "<td style='padding:3px 8px;font-size:12px;text-align:right;font-family:DM Mono,monospace'"
            " id='dv_" + col + "'>" + dv + "</td>"
            "</tr>"
        )

    # Checklist items
    chk_items = [
        ("c1","1. Discount entered"),("c2","2. Staff on shift entered"),
        ("c3","3. Person cashing up entered"),("c4","4. Customer number entered"),
        ("c5","5. Print count entered"),("c6","6. CR1 entered"),
        ("c7","7. CR2 entered"),("c8","8. Paid out checked"),
        ("c9","9. Comments if needed"),("c10","10. Z-read number entered"),
        ("c11","11. Actual cash C/F entered"),("c12","12. All checks complete"),
    ]
    chk_html = ""
    for cid, clbl in chk_items:
        chk_html += (
            "<div style='display:flex;gap:8px;align-items:center;padding:6px 0;"
            "border-bottom:1px solid #f1f5f9;font-size:13px;color:#64748b'>"
            "<span id='" + cid + "' style='font-size:16px'>&#9744;</span>"
            "<span>" + clbl + "</span></div>"
        )

    # ZZ row for Thursday
    zz_row = ""
    if is_thurs:
        zz_row = (
            "&nbsp;&nbsp;&nbsp;<span style='font-size:12px;font-weight:700'>Till ZZ' No.:</span>"
            "&nbsp;<span id='z2_p_check' onclick='ztick(this)' style='font-size:16px;cursor:pointer'>&#9744;</span>"
            "&nbsp;<input type='number' name='z2_read_no' id='inp_z2' value='" + z2_cur + "'"
            " min='1' step='1' oninput='chkZ2();chks()' placeholder='ZZ'"
            " style='width:65px;text-align:center;border:2px solid #fefce8;border-radius:6px;"
            "padding:4px 6px;font-size:13px;font-weight:900;background:#fefce8;color:#0f2942'>"
            "&nbsp;<span id='z2_echo' style='font-size:12px;opacity:.7'></span>"
            "&nbsp;<span id='z2_tick' style='font-size:16px'>&#9744;</span>"
            "&nbsp;<span id='z2_status' style='font-size:11px;font-weight:700;color:#dc2626'>Enter ZZ number</span>"
        )

    cats_js   = repr([col for col,_,_,_ in SALES_CATEGORIES])
    cards_js  = repr([col for col,_ in CARD_TYPES])
    denoms_js = repr([(col,val) for col,val,_ in denoms])

    content = (
        "<div class='flex justify-between items-center flex-wrap gap-3'>"
        "<div><a href='/sales?store=" + store + "' style='color:#1e3a5f;font-size:13px;font-weight:700'>&#8592; Back</a>"
        "<div class='text-2xl font-black text-slate-800 mt-1'>&#128221; Daily Cash Sheet &mdash; " + store + "</div>"
        "<div style='font-size:13px;color:#64748b'>" + d_fmt + "</div></div>"
        "<div style='display:flex;gap:8px'>"
        "<a href='/sales/enter?store=" + store + "&date=" + prev_d + "' class='btn-secondary' style='padding:5px 12px'>&#8592; Prev</a>"
        "<a href='/sales/enter?store=" + store + "&date=" + next_d + "' class='btn-secondary' style='padding:5px 12px'>Next &#8594;</a>"
        "</div></div>"

        # Z read bar
        "<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;"
        "padding:6px 0 10px 0;border-bottom:2px solid #e2e8f0;width:fit-content;margin-bottom:12px'>"
        "<span style='font-size:12px;font-weight:700'>Till Z' No.:</span>"
        "&nbsp;<span id='z_p_check' onclick='ztick(this)' style='font-size:18px;cursor:pointer' title='Click to confirm Z entered'>&#9744;</span>"
        "&nbsp;<input type='number' name='z_read_no' id='inp_z' value='" + z_cur + "'"
        " min='1' step='1' oninput='chkZ();chks()' placeholder='Z No.'"
        " style='width:70px;text-align:center;border:2px solid #fefce8;border-radius:6px;"
        "padding:5px 6px;font-size:14px;font-weight:900;background:#fefce8;color:#0f2942'>"
        "&nbsp;<span id='z_tick' style='font-size:20px'>&#9744;</span>"
        "&nbsp;<span id='z_status' style='font-size:11px;font-weight:700;color:#dc2626'>Enter Z number to check</span>"
        + zz_row +
        "</div>"

        "<form action='/sales/enter' method='POST' id='salesForm'>"
        "<input type='hidden' name='store' value='" + store + "'>"
        "<input type='hidden' name='date' value='" + date + "'>"
        "<input type='hidden' name='prev_z' value='" + str(prev_z) + "'>"
        "<input type='hidden' name='prev_z2' value='" + str(prev_z2) + "'>"

        # Tab bar
        "<div style='display:flex;gap:0;border-bottom:2px solid #e2e8f0;margin-bottom:4px'>"
        "<button type='button' onclick='showTab(1)' id='tab1' class='tab-btn active'>"
        "&#128200; Sales &amp; Shift <span id='b1' class='tab-badge'>0/25</span></button>"
        "<button type='button' onclick='showTab(2)' id='tab2' class='tab-btn'>"
        "&#128179; Card Breakdown <span id='b2' class='tab-badge'>—</span></button>"
        "<button type='button' onclick='showTab(3)' id='tab3' class='tab-btn'>"
        "&#128181; Cash &amp; Recon <span id='b3' class='tab-badge'>—</span></button>"
        "</div>"
        "<div style='background:#e2e8f0;border-radius:99px;height:4px;margin-bottom:12px'>"
        "<div id='tab-progress' style='background:#0f2942;border-radius:99px;height:4px;width:25%;transition:width .3s'></div>"
        "</div>"

        # Tab 1: Sales
        "<div id='panel1' class='tab-panel'>"
        "<div style='display:flex;gap:20px;align-items:start'>"
        "<div style='display:flex;flex-direction:column;gap:12px'>"
        "<div class='card' style='padding:0;overflow:hidden;width:fit-content'>"
        "<div style='padding:8px 14px;background:#0f2942;color:white;font-weight:700;font-size:13px'>Sales by Category</div>"
        "<div>"
        "<table style='border-collapse:collapse;table-layout:fixed'>"
        "<col style='width:215px'>"
        "<col style='width:60px'>"
        "<col style='width:70px'>"
        "<col style='width:85px'>"
        "</colgroup>"
        "<thead><tr style='background:#f8fafc'>"
        "<th style='padding:5px 8px;text-align:left;font-size:10px;color:#64748b'>CATEGORY</th>"
        "<th style='padding:5px 3px;text-align:center;font-size:10px;color:#92400e;background:#fefce8'>TRANS</th>"
        "<th style='padding:5px 5px;text-align:center;font-size:10px;color:#166534;background:#f0fdf4'>PER TRANS</th>"
        "<th style='padding:5px 3px;text-align:right;font-size:10px;color:#92400e;background:#fefce8'>ACTUAL £</th>"
        "</tr></thead>"
        "<tbody>" + cat_rows + "</tbody>"
        "</table></div>"
        "<div style='padding:10px 16px;background:#0f2942;display:flex;justify-content:space-between;align-items:center'>"
        "<span style='font-weight:700;color:white;font-size:13px'>TOTAL SALES</span>"
        "<span id='tot_sales' style='font-weight:900;color:white;font-size:18px;font-family:DM Mono,monospace'>£0.00</span>"
        "</div></div>"
        "<div class='card' style='padding:0;overflow:hidden;max-width:432px'>"
        "<div style='padding:8px 14px;background:#0f2942;color:white;font-weight:700;font-size:13px;margin:0 0 0 0;border-radius:8px 8px 0 0'>Shift &amp; Till Details</div>"
        "<table style='width:100%;border-collapse:collapse'><tbody>"
        "<tr style='border-bottom:1px solid #f1f5f9'>"
        "<td style='padding:4px 6px;font-size:12px;font-weight:700;color:#64748b;white-space:nowrap;width:90px'>Staff on Shift</td>"
        "<td style='padding:4px 6px'><input type='text' name='staff_on_shift' value='" + fvs("staff_on_shift") + "' oninput='chks()'"
        " placeholder='e.g. Kaleem / Rhys / Jessica' style='width:100%;border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;box-sizing:border-box'></td></tr>"
        "<tr style='border-bottom:1px solid #f1f5f9'>"
        "<td style='padding:4px 8px;font-size:12px;font-weight:700;color:#64748b;white-space:nowrap'>Person Cashing Up</td>"
        "<td style='padding:4px 6px'><input type='text' name='person_cashing_up' value='" + fvs("person_cashing_up") + "' oninput='chks()'"
        " placeholder='One name only' style='width:100%;border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;box-sizing:border-box'></td></tr>"
        "<tr style='border-bottom:1px solid #f1f5f9'>"
        "<td style='padding:4px 8px;font-size:12px;font-weight:700;color:#64748b;white-space:nowrap'>Customer Count</td>"
        "<td style='padding:4px 6px'><input type='number' name='customer_count' value='" + fvi("customer_count") + "' oninput='chks()'"
        " placeholder='From till read' style='width:100%;border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;box-sizing:border-box'></td></tr>"
        "<tr style='border-bottom:1px solid #f1f5f9'>"
        "<td style='padding:4px 8px;font-size:12px;font-weight:700;color:#64748b;white-space:nowrap'>Print Count (D3000)</td>"
        "<td style='padding:4px 6px'><input type='number' name='print_count' value='" + fvi("print_count") + "' oninput='chks()'"
        " placeholder='From D3000' style='width:100%;border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;box-sizing:border-box'></td></tr>"
        "<tr>"
        "<td style='padding:4px 8px;font-size:12px;font-weight:700;color:#64748b;white-space:nowrap'>Apply &amp; Go Count</td>"
        "<td style='padding:4px 6px'><input type='number' name='apply_go_count' value='" + fvi("apply_go_count") + "'"
        " placeholder='e.g. 0' style='width:100%;border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;box-sizing:border-box'></td></tr>"
        "</tbody></table>"
        "</div>"
        "</div>"
        "<div style='position:sticky;top:16px'>"
        "<div class='card' style=''>"
        "<div style='padding:8px 14px;background:#0f2942;color:white;font-weight:700;font-size:13px;margin:-14px -14px 12px -14px;border-radius:8px 8px 0 0'>&#9989; Completion Checklist</div>"
        "<div style='font-size:12px;color:#94a3b8;margin-bottom:10px'>Complete all items before saving</div>"
        + chk_html +
        "<div style='margin-top:16px'>"
        "<button type='submit' name='action' value='save' id='sbtn'"
        " class='btn-primary' style='width:100%;padding:12px;font-size:15px'>&#128190; Save Cash Sheet</button>"
        "<div id='swarn' style='display:none;margin-top:8px;background:#fef3c7;border:1px solid #fcd34d;"
        "border-radius:8px;padding:10px;font-size:13px;color:#92400e'>&#9888; Complete all checklist items first</div>"
        "</div></div>"
        "</div>"

        "</div>"
        "</div>"
        "</div>"
        "<button type='button' onclick='showTab(2)' class='btn-primary'>Next: Card Breakdown &#8594;</button>"
        # Tab 2: Till & Cards
        "<div id='panel2' class='tab-panel' style='display:none'>"
        "<div style='max-width:950px'>"
        "<div class='grid gap-4' style='grid-template-columns:1fr 1fr'>"

        # Left: Shift details + Till reads
        "<div style='display:flex;flex-direction:column;gap:10px'>"

        "<div class='card' style='padding:0;overflow:hidden'>"
        "<div style='padding:8px 14px;background:#0f2942;color:white;font-weight:700;font-size:13px'>Total Credit Cards as per Till</div><table style='border-collapse:collapse;width:auto'><tbody><tr style='border-bottom:1px solid #f1f5f9'><td style='padding:6px 10px;font-size:13px;color:#334155;white-space:nowrap'>CR1 &mdash; Credit Card Sales</td><td style='padding:4px 8px;text-align:right'><input type='number' step='0.01' name='till_credit_sales' value='" + fv("till_credit_sales") + "' onblur='if(this.value&&!isNaN(this.value))this.value=parseFloat(this.value).toFixed(2)' oninput='updTillTotal();chks()' placeholder='0.00' style='width:80px;text-align:right;border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;font-family:DM Mono,monospace'></td></tr><tr style='border-bottom:1px solid #f1f5f9'><td style='padding:6px 10px;font-size:13px;color:#334155;white-space:nowrap'>CR2 &mdash; Internet Sales</td><td style='padding:4px 8px;text-align:right'><input type='number' step='0.01' name='till_internet_sales' id='cr2' value='" + fv("till_internet_sales") + "' onblur='if(this.value&&!isNaN(this.value))this.value=parseFloat(this.value).toFixed(2)' oninput='updTillTotal();chks()' placeholder='0.00' style='width:80px;text-align:right;border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;font-family:DM Mono,monospace'></td></tr><tr style='border-bottom:1px solid #e2e8f0;background:#f8fafc'><td style='padding:6px 10px;font-size:13px;font-weight:700;color:#0f2942'>Total CR1 + CR2</td><td style='padding:6px 10px;text-align:right;font-size:13px;font-weight:900;font-family:DM Mono,monospace;color:#0f2942' id='till_total'>£0.00</td></tr><tr style='border-bottom:1px solid #e2e8f0;background:#f8fafc'><td style='padding:6px 10px;font-size:13px;font-weight:700;color:#0f2942'>Total Cards (PDQ)</td><td style='padding:6px 10px;text-align:right;font-size:13px;font-weight:900;font-family:DM Mono,monospace;color:#0f2942' id='till_cards_ref'>£0.00</td></tr><tr style='background:#f8fafc'><td style='padding:6px 10px;font-size:13px;font-weight:700;color:#0f2942'>Difference</td><td style='padding:6px 10px;text-align:right;font-size:13px;font-weight:900;font-family:DM Mono,monospace' id='till_diff_display'>—</td></tr></tbody></table></div>"
        "<div id='card_chk' style='margin-top:6px;font-size:12px;display:none'></div>"
        "</div>"

        # Right: Card breakdown
        "<div class='card' style='padding:0;overflow:hidden'>"
        "<div style='padding:8px 14px;background:#0f2942;color:white;display:flex;justify-content:space-between;font-weight:700;font-size:13px'>Card Breakdown (PDQ)</div>"
        "<table style='width:100%;border-collapse:collapse'><tbody>" + card_rows + "</tbody>"
        "<tfoot><tr style='background:#f8fafc;border-top:2px solid #e2e8f0'>"
        "<td style='padding:6px 10px;font-weight:900;font-size:13px'>Total Cards</td>"
        "<td style='padding:6px 6px;text-align:right;font-weight:900;font-size:14px;font-family:DM Mono,monospace' id='card_tot2'>£0.00</td>"
        "</tr></tfoot></table></div>"
        "</div>"
        "</div>"
        "<div style='display:flex;justify-content:space-between;margin-top:10px'>"
        "<button type='button' onclick='showTab(1)' class='btn-secondary'>&#8592; Back: Sales &amp; Shift</button>"
        "<button type='button' onclick='showTab(3)' class='btn-primary'>Next: Cash &amp; Recon &#8594;</button>"
        "</div></div>"

        # Tab 3: Cash Count
        "<div id='panel3' class='tab-panel' style='display:none'>"
        "<div style='max-width:950px'>"
        "<div class='grid gap-4' style='grid-template-columns:1fr 1fr'>"

        # Left: Denominations
        "<div class='card' style='padding:0;overflow:hidden'>"
        "<div style='padding:8px 14px;background:#0f2942;color:white;display:flex;justify-content:space-between;font-weight:700;font-size:13px'>"
        "Cash Count by Denomination<span id='cash_count_total' style='font-family:DM Mono,monospace'>£0.00</span></div>"
        "<table style='width:100%;border-collapse:collapse'>"
        "<thead><tr style='background:#f8fafc'>"
        "<th style='padding:3px 8px;text-align:left;font-size:10px;color:#64748b'>DENOM</th>"
        "<th style='padding:3px 4px;text-align:center;font-size:10px;color:#64748b'>COUNT</th>"
        "<th style='padding:3px 8px;text-align:right;font-size:10px;color:#64748b'>VALUE</th>"
        "</tr></thead>"
        "<tbody>" + denom_rows + "</tbody>"
        "<tfoot>"
        "<tr style='background:#f0f9ff'>"
        "<td colspan='2' style='padding:4px 8px;font-size:11px;font-weight:700;color:#0369a1'>Notes Tin</td>"
        "<td style='padding:4px 4px;text-align:right'>"
        "<input type='number' step='0.01' name='notes_tin' id='notes_tin_inp'"
        " value='" + fv("notes_tin") + "' oninput='updCashStore()' placeholder='0.00'"
        " style='width:80px;text-align:right;border:1px solid #bae6fd;border-radius:5px;padding:3px 5px;font-size:12px;font-family:DM Mono,monospace'>"
        "</td></tr>"
        "<tr style='background:#f0fdf4'>"
        "<td colspan='2' style='padding:4px 8px;font-size:11px;font-weight:700;color:#166534'>Change Tin</td>"
        "<td style='padding:4px 4px;text-align:right'>"
        "<input type='number' step='0.01' name='change_tin' id='change_tin_inp'"
        " value='" + fv("change_tin") + "' oninput='updCashStore()' placeholder='0.00'"
        " style='width:80px;text-align:right;border:1px solid #bbf7d0;border-radius:5px;padding:3px 5px;font-size:12px;font-family:DM Mono,monospace'>"
        "</td></tr>"
        "<tr style='border-top:2px solid #e2e8f0;background:#f8fafc'>"
        "<td colspan='2' style='padding:5px 8px;font-size:12px;font-weight:900;color:#0f2942'>Total Cash in Store</td>"
        "<td style='padding:5px 8px;text-align:right;font-size:14px;font-weight:900;color:#0f2942;font-family:DM Mono,monospace' id='total_cash_store'>£0.00</td>"
        "</tr></tfoot></table>"
        "<input type='hidden' name='notes_tin' id='hid_notes_tin'>"
        "<input type='hidden' name='change_tin' id='hid_change_tin'>"
        "<input type='hidden' name='total_cash_store' id='hid_total_cash'>"
        "</div>"

        # Right: Cash reconciliation
        "<div style='display:flex;flex-direction:column;gap:10px'>"
        "<div class='card'>"
        "<div style='padding:8px 14px;background:#0f2942;color:white;font-weight:700;font-size:13px;margin:-14px -14px 12px -14px;border-radius:8px 8px 0 0'>Cash Reconciliation</div>"
        "<div class='grid gap-2' style='grid-template-columns:1fr 1fr'>"
        "<div><label>Opening Cash B/F <span style='font-weight:400;color:#94a3b8'>(auto)</span></label>"
        "<input type='number' step='0.01' name='opening_cash_bf'"
        " value='" + ("%.2f" % bf_auto if bf_auto else "") + "'"
        " oninput='updCash()' placeholder='From yesterday'"
        " style='border:1px solid #bae6fd;border-radius:6px;padding:6px 8px;font-size:13px;width:100%;text-align:right;font-family:DM Mono,monospace;background:#f0f9ff'></div>"
        "<div><label>Paid Out Total</label>"
        "<input type='number' step='0.01' name='paid_out_total' value='" + fv("paid_out_total") + "'"
        " id='inp_paidout' oninput='chks();updCash()' placeholder='0.00'"
        " style='border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;width:100%;text-align:right;font-family:DM Mono,monospace'></div>"
        "<div style='grid-column:1/-1'><label>Paid Out Details</label>"
        "<textarea name='paid_out_notes' rows='2' placeholder='e.g. Cleaning £5.00'"
        " style='border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:12px;width:100%'>" + fvs("paid_out_notes") + "</textarea></div>"
        "</div>"
        "<div style='margin-top:8px;background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:8px'>"
        "<label style='display:flex;gap:8px;align-items:center;cursor:pointer;text-transform:none;font-size:13px;font-weight:600;color:#166534'>"
        "<input type='checkbox' name='paid_out_checked' id='po_chk' " + ("checked" if e.get("paid_out_checked") else "") + " oninput='chks()'"
        " style='width:18px;height:18px'>"
        "I confirm paid out sheet has been checked and total is correct"
        "</label></div>"
        "<div style='margin-top:8px;padding:8px;background:#f8fafc;border-radius:8px;font-size:12px;line-height:1.9'>"
        "<div style='display:flex;justify-content:space-between'><span>Total Sales</span><span id='r_sales' style='font-family:DM Mono,monospace'>£0.00</span></div>"
        "<div style='display:flex;justify-content:space-between'><span>+ Opening B/F</span><span id='r_bf' style='font-family:DM Mono,monospace'>£0.00</span></div>"
        "<div style='display:flex;justify-content:space-between;border-top:1px solid #e2e8f0;padding-top:2px;font-weight:700'><span>Sub Total</span><span id='r_sub' style='font-family:DM Mono,monospace'>£0.00</span></div>"
        "<div style='display:flex;justify-content:space-between'><span>- Paid Out</span><span id='r_po' style='font-family:DM Mono,monospace;color:#dc2626'>£0.00</span></div>"
        "<div style='display:flex;justify-content:space-between'><span>- Total Cards</span><span id='r_cards' style='font-family:DM Mono,monospace;color:#dc2626'>£0.00</span></div>"
        "<div style='display:flex;justify-content:space-between;border-top:1px solid #e2e8f0;padding-top:2px;font-weight:900'><span>Theoretical Cash</span><span id='r_theo' style='font-family:DM Mono,monospace'>£0.00</span></div>"
        "<div style='display:flex;justify-content:space-between;font-weight:700'><span>Difference</span><span id='r_diff' style='font-family:DM Mono,monospace'>—</span></div>"
        "</div>"
        "<div class='grid gap-2' style='grid-template-columns:1fr 1fr;margin-top:8px'>"
        "<div><label>Paid Into Bank</label>"
        "<input type='number' step='0.01' name='total_paid_bank' value='" + fv("total_paid_bank") + "'"
        " oninput='updCash()' placeholder='0.00'"
        " style='border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;width:100%;text-align:right;font-family:DM Mono,monospace'></div>"
        "<div><label>Actual Cash C/F Tomorrow</label>"
        "<input type='number' step='0.01' name='actual_cash_cf' value='" + fv("actual_cash_cf") + "'"
        " id='inp_cashcf' oninput='chks();updCash()' placeholder='0.00'"
        " style='border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;width:100%;text-align:right;font-family:DM Mono,monospace'></div>"
        "</div>"
        "<div id='diff_box' style='margin-top:6px;display:none'>"
        "<label style='color:#dc2626'>Reason for Difference (required)</label>"
        "<textarea name='till_diff_reason' rows='2' placeholder='Please explain'"
        " style='border:1px solid #fca5a5;border-radius:6px;padding:6px 8px;font-size:12px;width:100%;margin-top:3px'>" + fvs("till_diff_reason") + "</textarea></div>"
        "</div>"
        "<div class='card'><label style='font-size:13px;font-weight:700;color:#0f2942'>Comments</label>"
        "<textarea name='notes' rows='3' oninput='chks()'"
        " placeholder='Any notes, discrepancies or issues to report'"
        " style='border:1px solid #e2e8f0;border-radius:6px;padding:6px 8px;font-size:13px;width:100%;margin-top:6px'>" + fvs("notes") + "</textarea></div>"
        "</div>"
        "</div>"
        "<div style='display:flex;justify-content:space-between;margin-top:10px'>"
        "<button type='button' onclick='showTab(2)' class='btn-secondary'>&#8592; Back: Card Breakdown</button>"
        "</div></div>"

        "</div></form>"
    )

    # Add tab styles and JS
    tab_css = """<style>
tr.sales-row-active td {  background:#dbeafe !important;}.sales-row-active td input {  background:#bfdbfe !important;}.sales-table tr.discount-rowtr.sales-row-active td {  background:#fee2e2 !important;}.sales-table tr.discount-row.sales-row-active td input {  background:#fecaca !important;}.tab-btn{padding:9px 18px;font-size:13px;font-weight:700;color:#64748b;background:none;
  border:none;cursor:pointer;border-bottom:3px solid transparent;margin-bottom:-2px;
  font-family:'DM Sans',sans-serif;white-space:nowrap;}
.tab-btn:hover{color:#0f2942;}
.tab-btn.active{color:#0f2942;border-bottom-color:#0f2942;}
.tab-badge{background:#e2e8f0;color:#64748b;border-radius:99px;padding:1px 7px;
  font-size:11px;margin-left:6px;}
.tab-btn.active .tab-badge{background:#0f2942;color:white;}
.tab-btn.done .tab-badge{background:#16a34a;color:white;}
</style>"""

    js_code = """
function highlightRow(el){  var tbl=el.closest('table');  if(tbl)tbl.querySelectorAll('.sales-row-active').forEach(function(r){r.classList.remove('sales-row-active');});  var row=el.closest('tr');  if(row)row.classList.add('sales-row-active');}function showTab(n){
  for(var i=1;i<=3;i++){
    document.getElementById('panel'+i).style.display=i===n?'block':'none';
    document.getElementById('tab'+i).classList.toggle('active',i===n);
  }
  document.getElementById('tab-progress').style.width=(n*33.3)+'%';
}
function ztick(t){
  var on=t.textContent==='\\u2610';
  t.innerHTML=on?'\\u2713':'\\u2610';
  t.style.color=on?'#16a34a':'';
}
function gv(n){var es=document.getElementsByName(n);return es.length?(parseFloat(es[0].value||0)||0):0;}
function gs(n){var es=document.getElementsByName(n);return es.length?es[0].value.trim():'';}
function fm(n){return'\\xa3'+n.toFixed(2);}
function tk(id,ok){var e=document.getElementById(id);if(!e)return;
  e.innerHTML=ok?'&#10003;':'&#9744;';e.style.color=ok?'#16a34a':'#94a3b8';e.style.fontSize=ok?'18px':'15px';}

var CATS=CAT_PH;
var CARDS=CARD_PH;var ALL_CARDS=CARDS.concat(['card_internet_orders']);
var DENOMS=DENOM_PH;
var PREV_Z=PREVZ_PH;
var PREV_Z2=PREVZ2_PH;

function updVPT(col,tcol){
  var a=parseFloat(document.getElementsByName(col)[0]?.value||0)||0;
  var t=parseInt(document.getElementsByName(tcol)[0]?.value||0)||0;
  var el=document.getElementById('vpt_'+col);
  if(el)el.textContent=t>0?(a/t).toFixed(2):'\\u2014';
}
function updDiscVPT(){
  var a=parseFloat(document.getElementsByName('discount_amount')[0]?.value||0)||0;
  var t=parseInt(document.getElementsByName('discount_trans')[0]?.value||0)||0;
  var el=document.getElementById('vpt_discount');
  if(el)el.textContent=t>0?(a/t).toFixed(2):'\\u2014';
}
function updTot(){var io=parseFloat(document.getElementsByName('internet_orders')[0]?.value||0)||0;var ioF=document.getElementById('card_io_field');if(ioF){ioF.value=io>0?io.toFixed(2):'';updCards();}  updTillTotal();
  var t=0;
  CATS.forEach(function(c){var es=document.getElementsByName(c);if(es.length)t+=parseFloat(es[0].value||0)||0;});
  document.getElementById('tot_sales').textContent=fm(t);
  var filled=0;
  CATS.forEach(function(c){var es=document.getElementsByName(c);if(es.length&&es[0].value)filled++;});
  document.getElementById('b1').textContent=filled+'/25';
  updCash();chks();
}
function updTillTotal(){var cr1=parseFloat(document.getElementsByName('till_credit_sales')[0]?.value||0)||0;var cr2=parseFloat(document.getElementsByName('till_internet_sales')[0]?.value||0)||0;var tot=cr1+cr2;var tt=document.getElementById('till_total');if(tt)tt.textContent='\xa3'+tot.toFixed(2);var cards=0;ALL_CARDS.forEach(function(c){var es=document.getElementsByName(c);if(es.length)cards+=parseFloat(es[0].value||0)||0;});var tr=document.getElementById('till_cards_ref');if(tr)tr.textContent='\xa3'+cards.toFixed(2);var diff=tot-cards;var de=document.getElementById('till_diff_display');if(tot>0||cards>0){if(Math.abs(diff)<0.01){de.textContent='\u2713 Balanced';de.style.color='#16a34a';}else{de.textContent=(diff>0?'\u26a0 CR1+CR2 exceeds PDQ by \xa3':'\u26a0 PDQ exceeds CR1+CR2 by \xa3')+Math.abs(diff).toFixed(2);de.style.color='#dc2626';}}else{de.textContent='\u2014';de.style.color='';}}function updCards(){
  var t=0;
  ALL_CARDS.forEach(function(c){var es=document.getElementsByName(c);if(es.length)t+=parseFloat(es[0].value||0)||0;});
  var ct=document.getElementById('card_tot');if(ct)ct.textContent=fm(t);
  var ct2=document.getElementById('card_tot2');if(ct2)ct2.textContent=fm(t);
  var tcr=document.getElementById('till_cards_ref');if(tcr)tcr.textContent=fm(t);
  updTillTotal();
  updCash();
}
function updDenoms(){
  var notes=0,coins=0;
  DENOMS.forEach(function(d){
    var col=d[0],val=d[1];
    var inps=document.getElementsByName(col);
    var cnt=inps.length?(parseInt(inps[0].value)||0):0;
    var amt=cnt*val;
    var dv=document.getElementById('dv_'+col);
    if(dv)dv.textContent=cnt?fm(amt):'\\u2014';
    if(val>=5)notes+=amt;else coins+=amt;
  });
  document.getElementById('cash_count_total').textContent=fm(notes+coins);
  updCashStore();
}
function updCashStore(){
  var notes=0,coins=0;
  DENOMS.forEach(function(d){
    var inps=document.getElementsByName(d[0]);
    var cnt=inps.length?(parseInt(inps[0].value)||0):0;
    if(d[1]>=5)notes+=cnt*d[1];else coins+=cnt*d[1];
  });
  var ntin=parseFloat(document.getElementById('notes_tin_inp')?.value||0)||0;
  var ctin=parseFloat(document.getElementById('change_tin_inp')?.value||0)||0;
  var tot=(notes+coins)+ntin+ctin;
  document.getElementById('total_cash_store').textContent=fm(tot);
  document.getElementById('hid_notes_tin').value=ntin.toFixed(2);
  document.getElementById('hid_change_tin').value=ctin.toFixed(2);
  document.getElementById('hid_total_cash').value=tot.toFixed(2);
}
function updCash(){
  var sales=0;
  CATS.forEach(function(c){var es=document.getElementsByName(c);if(es.length)sales+=parseFloat(es[0].value||0)||0;});
  var cards=0;
  CARDS.forEach(function(c){var es=document.getElementsByName(c);if(es.length)cards+=parseFloat(es[0].value||0)||0;});
  var bf=gv('opening_cash_bf'),po=gv('paid_out_total');
  var sub=sales+bf,theo=sub-po-cards;
  var cf=gv('actual_cash_cf'),diff=cf-theo;
  document.getElementById('r_sales').textContent=fm(sales);
  document.getElementById('r_bf').textContent=fm(bf);
  document.getElementById('r_sub').textContent=fm(sub);
  document.getElementById('r_po').textContent=fm(po);
  document.getElementById('r_cards').textContent=fm(cards);
  document.getElementById('r_theo').textContent=fm(theo);
  var de=document.getElementById('r_diff');
  if(cf>0){de.textContent=(diff>=0?'+':'')+fm(diff);de.style.color=Math.abs(diff)<0.01?'#16a34a':'#dc2626';
    document.getElementById('diff_box').style.display=Math.abs(diff)>0.01?'block':'none';}
  else{de.textContent='\\u2014';de.style.color='';document.getElementById('diff_box').style.display='none';}
}
function chkZ(){
  var zel=document.getElementsByName('z_read_no');
  var z=zel.length?(parseInt(zel[0].value)||0):0;
  var el=document.getElementById('z_status');if(!el)return;
  if(!z){el.textContent='Enter Z number to check';el.style.color='#dc2626';return;}
  if(PREV_Z===0){el.textContent='No previous Z to compare';el.style.color='#d97706';return;}
  if(z===PREV_Z+1){el.style.color='#16a34a';el.textContent='\\u2713 Z No. OK ('+PREV_Z+'\\u2192'+z+')';}
  else{el.style.color='#dc2626';el.textContent='\\u26a0 Expected Z '+(PREV_Z+1)+' got '+z+' \\u2014 comment required';}
}
function chkZ2(){
  var zel=document.getElementsByName('z2_read_no');
  var z2=zel.length?(parseInt(zel[0].value)||0):0;
  var el=document.getElementById('z2_status');if(!el)return;
  if(!z2){el.textContent='Enter ZZ number';el.style.color='#dc2626';return;}
  if(PREV_Z2===0){el.textContent='No previous ZZ';el.style.color='#d97706';return;}
  if(z2===PREV_Z2+1){el.style.color='#16a34a';el.textContent='\\u2713 ZZ No. OK';}
  else{el.style.color='#dc2626';el.textContent='\\u26a0 Expected ZZ '+(PREV_Z2+1);}
}
function chks(){
  var de=document.getElementsByName('discount_amount');
  tk('c1',de.length&&de[0].value!=='');
  tk('c2',gs('staff_on_shift').length>1);
  tk('c3',gs('person_cashing_up').length>1);
  tk('c4',gv('customer_count')>0);
  var pe=document.getElementsByName('print_count');tk('c5',pe.length&&pe[0].value!=='');
  var r1=document.getElementsByName('till_credit_sales');tk('c6',r1.length&&r1[0].value!=='');
  var r2=document.getElementsByName('till_internet_sales');tk('c7',r2.length&&r2[0].value!=='');
  var poc=document.getElementById('po_chk');tk('c8',poc&&poc.checked);
  tk('c9',true);
  var zel=document.getElementsByName('z_read_no');tk('c10',zel.length&&parseInt(zel[0].value||0)>0);
  var cfe=document.getElementsByName('actual_cash_cf');var c11ok=cfe.length&&cfe[0].value!=='';tk('c11',c11ok);
  var r1v=r1.length&&r1[0].value!=='';
  var allOk=gs('staff_on_shift').length>1&&gs('person_cashing_up').length>1&&
    gv('customer_count')>0&&zel.length&&parseInt(zel[0].value||0)>0&&c11ok&&r1v;
  tk('c12',allOk);
  var done=0;for(var i=1;i<=12;i++){var e=document.getElementById('c'+i);if(e&&e.textContent==='\\u2713')done++;}
  document.getElementById('b4').textContent=done+'/12';
  if(done>=10)document.getElementById('tab3').classList.add('done');
  var btn=document.getElementById('sbtn'),wrn=document.getElementById('swarn');
  if(btn){btn.style.opacity=allOk?'1':'0.6';
    btn.onclick=function(ev){if(!allOk){ev.preventDefault();wrn.style.display='block';return false;}wrn.style.display='none';};}
}
document.addEventListener('DOMContentLoaded',function(){
  updTot();updCards();updDenoms();chks();chkZ();
  ALL_CARDS.forEach(function(c){var es=document.getElementsByName(c);if(es.length&&es[0]&&!es[0].readOnly)es[0].addEventListener('input',updCards);});
  var cr2=document.getElementsByName('till_internet_sales');
  if(cr2.length)cr2[0].addEventListener('input',function(){this.dataset.manual='1';});
});
"""

    js_code = (js_code
        .replace("CAT_PH", cats_js)
        .replace("CARD_PH", cards_js)
        .replace("DENOM_PH", denoms_js)
        .replace("PREVZ_PH", str(prev_z))
        .replace("PREVZ2_PH", str(prev_z2))
    )

    full_content = tab_css + content + "<script>" + js_code + "</script>"
    return page("Daily Cash Entry", full_content, user, "sales")


@router.post("/sales/enter")
async def save_sales_entry(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir

    form  = await request.form()
    store = form.get("store","")
    date  = form.get("date","")

    def fn(k):
        try: return float(form.get(k, 0) or 0)
        except: return 0.0
    def fi(k):
        try: return int(form.get(k, 0) or 0)
        except: return 0

    # All numeric columns
    num_cols = (
        [col for col,_,_,_ in SALES_CATEGORIES] +
        ["discount_amount"] +
        [col for col,_ in CARD_TYPES] +
        ["opening_cash_bf","paid_out_total","till_credit_sales","till_internet_sales",
         "total_cash_store","notes_tin","change_tin","total_paid_bank","actual_cash_cf"]
    )
    int_cols = ["notes_50","notes_20","notes_10","notes_5","coins_2","coins_1",
                "coins_50p","coins_20p","coins_10p","coins_5p","coins_2p","coins_1p"] + ["trans_"+col for col,_,_,_ in SALES_CATEGORIES]

    all_cols   = num_cols + int_cols
    num_values = [fn(c) for c in num_cols]
    int_values = [fi(c) for c in int_cols]
    values     = num_values + int_values

    z_read     = fi("z_read_no") or None
    z2_read    = fi("z2_read_no") or None
    notes      = str(form.get("notes","") or "").strip() or None
    po_notes   = str(form.get("paid_out_notes","") or "").strip() or None
    diff_reason= str(form.get("till_diff_reason","") or "").strip() or None
    z2_comment = str(form.get("z2_diff_comment","") or "").strip() or None
    po_checked = 1 if form.get("paid_out_checked") else 0
    staff_shift= str(form.get("staff_on_shift","") or "").strip() or None
    cashup_by  = str(form.get("person_cashing_up","") or "").strip() or None
    cust_count = fi("customer_count") or None
    print_count= fi("print_count") or None
    apply_go   = fi("apply_go_count") or None
    entered_by = user.get("username","")

    set_clause = ", ".join(f"{c}=?" for c in all_cols)
    col_list   = ", ".join(all_cols)
    ph         = ", ".join("?" for _ in all_cols)

    q(f"""INSERT INTO daily_cashsheet
            (store_name, sale_date, z_read_no, z2_read_no, {col_list},
             paid_out_notes, notes, till_diff_reason, z2_diff_comment,
             paid_out_checked, staff_on_shift, person_cashing_up,
             customer_count, print_count, apply_go_count, entered_by)
         VALUES(?,?,?,?,{ph},?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(store_name, sale_date) DO UPDATE SET
            z_read_no=excluded.z_read_no,
            z2_read_no=excluded.z2_read_no,
            {set_clause},
            paid_out_notes=excluded.paid_out_notes,
            notes=excluded.notes,
            till_diff_reason=excluded.till_diff_reason,
            z2_diff_comment=excluded.z2_diff_comment,
            paid_out_checked=excluded.paid_out_checked,
            staff_on_shift=excluded.staff_on_shift,
            person_cashing_up=excluded.person_cashing_up,
            customer_count=excluded.customer_count,
            print_count=excluded.print_count,
            apply_go_count=excluded.apply_go_count,
            entered_by=excluded.entered_by""",
      [store, date, z_read, z2_read] + values +
      [po_notes, notes, diff_reason, z2_comment, po_checked,
       staff_shift, cashup_by, cust_count, print_count, apply_go, entered_by] +
      values)

    from urllib.parse import quote as uq
    week_start = get_week_start(date)
    return RedirectResponse(
        f"/sales?store={store}&week_start={week_start}&msg={uq('Cash sheet saved for ' + date)}",
        status_code=303)


@router.get("/sales/targets", response_class=HTMLResponse)
def sales_targets(
    store:   str = "Uxbridge",
    year:    int = 0,
    session: str | None = Cookie(default=None),
    msg:     str = ""
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner","manager"):
        return RedirectResponse("/sales", status_code=303)

    if not year: year = datetime.now().year

    targets = q("SELECT * FROM sales_targets WHERE store_name=? AND year=? ORDER BY month",
                (store, year), fetch=True) or []
    tmap    = {dict(t)["month"]: dict(t) for t in targets}

    flash = f"<div class='flash-success'>{msg}</div>" if msg else ""

    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    rows = ""
    for m in range(1, 13):
        t       = tmap.get(m, {})
        target  = t.get("target_amount", 0)
        ly      = t.get("ly_actual", 0)
        rows += f"""<tr style='border-bottom:1px solid #f1f5f9'>
          <td style='padding:8px 12px;font-weight:700'>{months[m-1]}</td>
          <td style='padding:4px 8px'>
            <input type='number' step='0.01' form='targets_form' name='target_{m}'
              value='{"%.2f" % target if target else ""}'
              placeholder='0.00'
              style='width:100%;text-align:right;border:1px solid #e2e8f0;border-radius:6px;
                     padding:6px 8px;font-size:13px;font-family:DM Mono,monospace'>
          </td>
          <td style='padding:4px 8px'>
            <input type='number' step='0.01' form='targets_form' name='ly_{m}'
              value='{"%.2f" % ly if ly else ""}'
              placeholder='0.00'
              style='width:100%;text-align:right;border:1px solid #e2e8f0;border-radius:6px;
                     padding:6px 8px;font-size:13px;font-family:DM Mono,monospace'>
          </td>
        </tr>"""

    store_btns = ""
    for sv in ["Uxbridge","Newbury"]:
        cls = "btn-primary" if sv == store else "btn-secondary"
        store_btns += f"<a href='/sales/targets?store={sv}&year={year}' class='{cls}' style='padding:5px 14px;font-size:13px'>{sv}</a>"

    content = f"""
    {flash}
    <div class='flex justify-between items-center flex-wrap gap-3'>
      <div>
        <a href='/sales?store={store}' style='color:#1e3a5f;font-size:13px;font-weight:700'>&#8592; Back to Sales</a>
        <div class='text-2xl font-black text-slate-800 mt-1'>&#127919; Sales Targets — {store} {year}</div>
      </div>
      <div style='display:flex;gap:8px'>
        {store_btns}
        <a href='/sales/targets?store={store}&year={year-1}' class='btn-secondary' style='padding:5px 12px'>&#8592; {year-1}</a>
        <a href='/sales/targets?store={store}&year={year+1}' class='btn-secondary' style='padding:5px 12px'>{year+1} &#8594;</a>
      </div>
    </div>
    <form id='targets_form' action='/sales/targets' method='POST'>
      <input type='hidden' name='store' value='{store}'>
      <input type='hidden' name='year'  value='{year}'>
      <div class='card' style='padding:0;overflow:hidden'>
        <table style='width:100%;border-collapse:collapse;font-family:DM Sans,sans-serif'>
          <thead><tr style='background:#0f2942;color:white'>
            <th style='padding:10px 12px;text-align:left;font-size:12px'>Month</th>
            <th style='padding:10px 8px;text-align:right;font-size:12px'>Target (£)</th>
            <th style='padding:10px 8px;text-align:right;font-size:12px'>Last Year Actual (£)</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div style='margin-top:12px'>
        <button type='submit' class='btn-primary'>&#128190; Save Targets</button>
      </div>
    </form>"""

    return page("Sales Targets", content, user, "sales")


@router.post("/sales/targets")
async def save_targets(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    form  = await request.form()
    store = form.get("store","")
    year  = int(form.get("year", datetime.now().year))

    for m in range(1, 13):
        try: target = float(form.get(f"target_{m}", 0) or 0)
        except: target = 0
        try: ly = float(form.get(f"ly_{m}", 0) or 0)
        except: ly = 0
        q("""INSERT INTO sales_targets (store_name,year,month,target_amount,ly_actual)
             VALUES(?,?,?,?,?)
             ON CONFLICT(store_name,year,month) DO UPDATE SET
                target_amount=excluded.target_amount,
                ly_actual=excluded.ly_actual""",
          (store, year, m, target, ly))

    from urllib.parse import quote as uq
    return RedirectResponse(
        f"/sales/targets?store={store}&year={year}&msg={uq('Targets saved')}",
        status_code=303)


@router.get("/sales/franchise-return", response_class=HTMLResponse)
def franchise_return(store: str="", week_start: str="", session: str|None=Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    content = f"""
    <div class='text-2xl font-black text-slate-800'>&#128196; Franchise Return — {store}</div>
    <div class='card text-center' style='padding:40px;color:#94a3b8'>
      <div style='font-size:40px;margin-bottom:12px'>&#128196;</div>
      <div style='font-weight:700;font-size:16px;color:#334155'>Coming Soon</div>
      <div style='font-size:13px;margin-top:8px'>
        The Franchise Return PDF will be generated here once daily sales data is entered for the full week.
      </div>
      <a href='/sales?store={store}&week_start={week_start}' class='btn-secondary' style='margin-top:16px;display:inline-block'>
        &#8592; Back to Sales
      </a>
    </div>"""
    return page("Franchise Return", content, user, "sales")


@router.get("/sales/managers-report", response_class=HTMLResponse)
def managers_report(store: str="", week_start: str="", session: str|None=Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    content = f"""
    <div class='text-2xl font-black text-slate-800'>&#128200; Manager's Report — {store}</div>
    <div class='card text-center' style='padding:40px;color:#94a3b8'>
      <div style='font-size:40px;margin-bottom:12px'>&#128200;</div>
      <div style='font-weight:700;font-size:16px;color:#334155'>Coming Soon</div>
      <div style='font-size:13px;margin-top:8px'>
        The Manager's Report will be generated here showing weekly performance vs targets and last year.
      </div>
      <a href='/sales?store={store}&week_start={week_start}' class='btn-secondary' style='margin-top:16px;display:inline-block'>
        &#8592; Back to Sales
      </a>
    </div>"""
    return page("Manager's Report", content, user, "sales")


ensure_sales_tables()
