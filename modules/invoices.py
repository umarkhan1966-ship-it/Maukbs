"""invoices routes."""
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

from urllib.parse import quote as urlquote

# Columns the invoice list can be sorted by (clickable headers). Keys are the
# short codes used in the URL; values are the safe SQL column to sort on.
# "added" is the default = newest invoice first.
SORT_COLUMNS = {
    "added":    "invoice_id",
    "seq":      "seq_no",
    "supplier": "supplier_name",
    "invno":    "invoice_number",
    "invdate":  "invoice_date",
    "due":      "due_date",
    "gross":    "gross_amount",
    "balance":  "balance",
    "status":   "is_paid",
}


def fmt_uk_date(s):
    """Display a stored ISO date (yyyy-mm-dd) as UK format dd/mm/yyyy.
    Storage stays ISO (sorts correctly); this is display-only. Returns a dash
    for blanks and leaves anything unparseable untouched."""
    if not s:
        return "—"
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return str(s)


def fmt_uk_dt(s):
    """Display a stored timestamp as UK 'dd/mm/yyyy HH:MM'. Falls back to a plain
    date for older date-only values, and a dash for blanks."""
    if not s:
        return "—"
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except Exception:
        return fmt_uk_date(s)


def _norm_spaces(s: str) -> str:
    """Re-insert spaces lost by PDFs that render words with no gaps, e.g.
    'MAUKBsRealEstateLimited' -> 'MAUKBs Real Estate Limited',
    'VATNumber' -> 'VAT Number'. Only touches case boundaries so it never
    breaks codes like 'SAI1210'."""
    s = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s)          # camelCase boundary
    s = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', s)     # ACRONYMWord boundary
    return re.sub(r'\s+', ' ', s).strip()


def _parse_date(s: str):
    """Parse the many date shapes suppliers use into YYYY-MM-DD, or None.
    Handles compact forms like '31May2026' as well as '31 May 2026',
    '07/06/2026', '7 Jun 2026'."""
    from datetime import datetime as dt
    s = s.strip()
    m = re.search(r'(\d{1,2})\s*([A-Za-z]{3,9})\s*(\d{4})', s)
    if m:
        try:
            return dt.strptime(f"{int(m.group(1))} {m.group(2)[:3].title()} {m.group(3)}",
                               "%d %b %Y").strftime("%Y-%m-%d")
        except Exception:
            pass
    m = re.search(r'(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})', s)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = "20" + y
        try:
            return dt(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


# Our own company never appears as the *supplier* — these invoices are billed
# TO us, so any name containing this is the customer and must be ignored.
# (New MAUKBs companies are still caught by "maukbs"; the Uxbridge LLP is the
# only own-entity this misses, and it isn't billed for property expenses.)
_OWN_COMPANY_HINTS = ("maukbs",)


def entity_name(code: str, default: str = "") -> str:
    """Legal name of one of our own companies, from the company_entities table
    (single source of truth), so names/spelling stay consistent everywhere."""
    rows = q("SELECT legal_name FROM company_entities WHERE entity_code=?", (code,), fetch=True)
    return rows[0]["legal_name"] if rows else default

# Label patterns: each captures any inline value after the label on the SAME
# line; if that's empty we fall back to the value in the cell directly below
# (handles the very common two-column "label above value" invoice layout).
_FIELD_LABELS = {
    "invoice_number": r'^(?:tax\s+)?invoice\s*(?:no|number|num|#)\.?\s*[:\-]?\s*(.*)$',
    "receipt_number": r'^(?:vat\s+)?receipt\s*(?:no|number|#)\.?\s*[:\-]?\s*(.*)$',
    "invoice_date":   r'^(?:invoice\s*date|date\s*of\s*invoice|date)\s*[:\-]?\s*(.*)$',
    "due_date":       r'^(?:due\s*date|payment\s*due)\s*[:\-]?\s*(.*)$',
    "payment_terms":  r'^(?:payment\s*terms|terms)\s*[:\-]?\s*(.*)$',
}


def _layout_cells(pdf_bytes: bytes):
    """Return (cells, page_width) where cells is a flat list of
    (top, x0, col_index, text) reconstructed column-by-column so that label
    and value alignment is preserved."""
    import pdfplumber
    from collections import defaultdict
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page  = pdf.pages[0]
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        page_w = float(page.width or 600)

    if not words:
        return [], page_w

    # Cluster x0 positions into vertical column bands.
    xs = sorted({round(w["x0"]) for w in words})
    bands = []
    for x in xs:
        if bands and x - bands[-1][1] <= 25:
            bands[-1][1] = x
        else:
            bands.append([x, x])

    def col_of(x):
        x = round(x)
        for i, (a, b) in enumerate(bands):
            if a - 1 <= x <= b + 1:
                return i
        return -1

    colwords = defaultdict(list)
    for w in words:
        colwords[col_of(w["x0"])].append(w)

    cells = []
    for ci, ws in colwords.items():
        ws.sort(key=lambda w: (w["top"], w["x0"]))
        row, row_top = [], None
        groups = []
        for w in ws:
            if row and abs(w["top"] - row_top) <= 6:
                row.append(w)
            else:
                if row:
                    groups.append(row)
                row, row_top = [w], w["top"]
        if row:
            groups.append(row)
        for g in groups:
            text = _norm_spaces(" ".join(x["text"] for x in g))
            cells.append((g[0]["top"], min(x["x0"] for x in g), ci, text))
    return cells, page_w


def extract_pdf_data(pdf_bytes: bytes) -> dict:
    """Extract invoice fields from a PDF. Layout-aware: reconstructs the page
    by columns so labels pair with the right values even in multi-column
    invoices. Anything we cannot read confidently is simply left out, so the
    caller (and store staff) can fill it in manually."""
    result = {}
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        cells, page_w = _layout_cells(pdf_bytes)

        # Build per-column ordered cell lists so we can look "below" a label.
        from collections import defaultdict
        by_col = defaultdict(list)
        for top, x0, ci, txt in cells:
            by_col[ci].append((top, x0, txt))
        for ci in by_col:
            by_col[ci].sort(key=lambda c: c[0])

        def looks_like_label(t):
            low = t.lower().strip(" :-")
            return any(re.match(p, low) for p in _FIELD_LABELS.values())

        # ── Pass 1: layout-aware label -> value (inline, else cell below) ──
        for ci, col in by_col.items():
            for i, (top, x0, txt) in enumerate(col):
                low = txt.lower().strip()
                for field, pat in _FIELD_LABELS.items():
                    if result.get(field):
                        continue
                    m = re.match(pat, low)
                    if not m:
                        continue
                    val = (m.group(1) or "").strip(" :-")
                    if not val and i + 1 < len(col):
                        nxt = col[i + 1][2].strip()
                        if not looks_like_label(nxt):
                            val = nxt
                    if not val:
                        continue
                    # Use the original (properly-cased) text for the value.
                    if field in ("invoice_date", "due_date"):
                        d = _parse_date(val)
                        if d:
                            result[field] = d
                    elif field == "payment_terms":
                        tm = re.search(r"\d+", val)
                        if tm:
                            result[field] = int(tm.group(0))
                    else:  # invoice_number / receipt_number
                        v = val.split()[0] if val else ""
                        if len(v) >= 2 and any(c.isdigit() for c in v) \
                           and "maukbs" not in v.lower():
                            result[field] = v

        # "VAT receipt number" style docs: use it as the invoice number.
        if not result.get("invoice_number") and result.get("receipt_number"):
            result["invoice_number"] = result["receipt_number"]
        result.pop("receipt_number", None)

        # ── Supplier name: top header block, never our own company ──
        header = [(top, x0, txt) for (top, x0, ci, txt) in cells if top < 260]
        header.sort(key=lambda c: (-c[1], c[0]))  # prefer right-side blocks first
        suffixes = ("ltd", "limited", "plc", "llp", "& co", "group",
                    "associates", "accountancy", "services")
        def bad_name(t):
            low = t.lower()
            # Reject our own company, labels, and run-on address blocks — a real
            # supplier name is short and comma-free; anything long/comma-heavy or
            # with "c/o" is an address we must not guess at (leave blank instead).
            return (any(h in low for h in _OWN_COMPANY_HINTS)
                    or looks_like_label(t)
                    or low.strip() in ("invoice", "tax invoice", "statement", "vat receipt")
                    or bool(re.match(r'^[\d\W]', t))
                    or "," in t or "c/o" in low or len(t) > 45 or len(t.split()) > 6)
        # Try a name (optionally merged with the next cell carrying the suffix).
        col_seq = defaultdict(list)
        for top, x0, ci, txt in cells:
            col_seq[ci].append((top, txt))
        for ci in col_seq:
            col_seq[ci].sort()
        for top, x0, ci, txt in sorted(cells, key=lambda c: (-c[1], c[0])):
            if top >= 260 or bad_name(txt):
                continue
            cand = txt
            seq = col_seq[ci]
            idx = next((k for k, (t, x) in enumerate(seq) if t == top and x == txt), None)
            # Append a following cell that carries (or is) the legal suffix,
            # e.g. 'Smartax Accountancy' + 'Ltd' -> 'Smartax Accountancy Ltd'.
            if idx is not None and idx + 1 < len(seq) and not bad_name(seq[idx + 1][1]):
                nxt = seq[idx + 1][1]
                is_bare_suffix = nxt.lower().strip(" .") in ("ltd", "limited", "plc", "llp")
                if is_bare_suffix or (not any(s in cand.lower() for s in suffixes)
                                      and any(s in nxt.lower() for s in suffixes)):
                    cand = f"{cand} {nxt}"
            if any(s in cand.lower() for s in suffixes):
                result["supplier_name"] = cand
                break
        if not result.get("supplier_name"):
            for top, x0, ci, txt in sorted(header, key=lambda c: c[0]):
                if not bad_name(txt) and len(txt) > 3:
                    result["supplier_name"] = txt
                    break

        # ── Amounts (regex on flat text; final totals first) ──
        def money(pats):
            for pat in pats:
                m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
                if m:
                    try:
                        return float(m.group(1).replace(",", ""))
                    except Exception:
                        pass
            return None

        gross = money([
            r"(?:grand\s*total|total\s*due|balance\s*due|amount\s*due|total\s*payable|total\s*gbp)[\s:£]*([0-9,]+\.[0-9]{2})",
            r"(?:total\s*inc\.?\s*vat|total\s*including\s*vat)[\s:£]*([0-9,]+\.[0-9]{2})",
            r"(?<![a-z])total\s*gbp[\s:£]*([0-9,]+\.[0-9]{2})",
        ])
        vat = money([
            r"total\s*vat\b.*?([0-9,]+\.[0-9]{2})",
            r"vat\s*@?\s*\d*\.?\d*%?\b.*?([0-9,]+\.[0-9]{2})",
        ])
        net = money([
            r"(?:sub\s*total|subtotal|net\s*total|amount\s*ex\.?\s*vat)[\s:£]*([0-9,]+\.[0-9]{2})",
        ])
        # Fill in the missing leg of gross / vat / net if two are known.
        if gross is not None and vat is not None and net is None:
            net = round(gross - vat, 2)
        elif net is not None and vat is not None and gross is None:
            gross = round(net + vat, 2)
        if gross is not None: result["gross_amount"] = gross
        if vat   is not None: result["vat_amount"]   = vat
        if net   is not None: result["net_amount"]   = net

        result["_raw_text"] = text[:500]  # first 500 chars for debugging

    except Exception as e:
        result["_error"] = str(e)

    return result


from core.paths import data_path
UPLOAD_DIR = data_path("invoice_pdfs")   # on the persistent volume when DATA_DIR is set


PAYMENT_METHODS = ["", "Direct Debit", "Card", "Cash", "Cheque", "Online", "Amex", "Credit Note"]


EXPENSE_TYPES   = [
    "Mortgage", "Insurance", "Legal Fees", "Management Fees",
    "Repairs & Maintenance", "Gas/Electric Certificate", "Inventory Fee",
    "Deposit Fee", "Tenancy Setup", "Rates", "Utilities", "Other"
]


def ledger_options(user: dict) -> list[tuple]:
    """Return (value, label) pairs for store/ledger selector.
    Staff see only their store. Managers see both stores.
    Owner sees both stores + all properties."""
    opts = []
    role  = user.get("role", "staff")
    store = user.get("store_name", "")
    if role == "owner":
        opts += [("Uxbridge", "🏪 Uxbridge (Retail)"),
                 ("Newbury",  "🏪 Newbury (Retail)")]
        props = q("SELECT short_name, full_address FROM properties ORDER BY short_name",
                  fetch=True) or []
        for p in props:
            opts.append((f"PROP:{p['short_name']}", f"🏠 {p['full_address']}"))
        # MREL = MAUKBs Real Estate Ltd: a company-level expenses ledger (not a
        # rental property), so it's added here rather than in the properties
        # table — keeps it out of rental/mortgage reports. Name from the entities table.
        opts.append(("PROP:MREL", f"🏢 {entity_name('MREL', 'MAUKBs Real Estate Ltd')} (Company)"))
    elif role == "manager":
        opts += [("Uxbridge", "🏪 Uxbridge (Retail)"),
                 ("Newbury",  "🏪 Newbury (Retail)")]
    else:
        # Store staff — only their assigned store
        s = store or "Uxbridge"
        opts += [(s, f"🏪 {s} (Retail)")]
    return opts


def is_property_ledger(store_val: str) -> bool:
    return store_val.startswith("PROP:")


def prop_name(store_val: str) -> str:
    return store_val.replace("PROP:", "")


def fetch_invoices(ledger: str, search: str, status: str,
                   pg: int, page_size: int = 30,
                   sort: str = "", direction: str = "desc"):
    is_prop = is_property_ledger(ledger)
    table   = "property_invoices" if is_prop else "supplier_invoices"
    loc_col = "property_name"     if is_prop else "store_name"
    loc_val = prop_name(ledger)   if is_prop else ledger

    conds  = [f"{loc_col} = ?"]
    params = [loc_val]

    if search.strip():
        conds.append("(supplier_name LIKE ? OR invoice_number LIKE ? OR CAST(seq_no AS TEXT) LIKE ? OR demand_ref LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"]

    today = datetime.now().strftime("%Y-%m-%d")
    if status == "overdue":
        # Direct Debit invoices auto-collect on/before the due date, so they are NOT
        # counted as "overdue" — they're awaiting the owner's statement reconciliation.
        conds.append(f"is_paid != 'Yes' AND due_date < '{today}' AND COALESCE(payment_method,'') != 'Direct Debit' AND COALESCE(approval_status,'approved')='approved'")
    elif status == "dd_reconcile":
        conds.append(f"is_paid != 'Yes' AND due_date < '{today}' AND COALESCE(payment_method,'')='Direct Debit'")
    elif status == "unpaid":
        conds.append("is_paid != 'Yes' AND COALESCE(approval_status,'approved')='approved'")
    elif status == "paid":
        conds.append("is_paid = 'Yes'")
    elif status == "partial":
        conds.append("is_paid != 'Yes' AND amount_paid > 0")
    elif status == "pending":
        conds.append("approval_status = 'pending'")
    elif status == "awaiting":
        conds.append("awaiting_invoice = 'Yes'")
    elif status == "credits":
        conds.append("gross_amount < 0 AND (linked_ref IS NULL OR linked_ref='')")
    elif status == "query":
        conds.append("under_query = 'Yes'")
    else:
        # Default: exclude pending from main view unless owner/manager reviewing
        pass

    where  = "WHERE " + " AND ".join(conds)
    total  = q(f"SELECT COUNT(*) as n FROM {table} {where}", params, fetch=True)
    total_n = total[0]["n"] if total else 0

    balance_expr = "COALESCE(gross_amount,0)-COALESCE(amount_paid,0)-COALESCE(credit_note,0)"

    # Build a safe ORDER BY from the whitelist. No sort given => newest first.
    col = SORT_COLUMNS.get(sort)
    direction_sql = "ASC" if str(direction).lower() == "asc" else "DESC"
    if col:
        order_by = f"{col} {direction_sql}, invoice_id DESC"
    else:
        order_by = "invoice_id DESC"   # default: most recently added on top

    rows = q(f"""
        SELECT *, {balance_expr} AS balance
        FROM {table} {where}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """, params + [page_size, (pg-1)*page_size], fetch=True) or []

    # Convert to dicts so .get() works safely throughout
    return [dict(r) for r in rows], total_n


@router.get("/invoices", response_class=HTMLResponse)
def invoices_page(
    session:  str | None = Cookie(default=None),
    ledger:   str = "Uxbridge",
    search:   str = "",
    status:   str = "",
    pg:       int = 1,
    edit_id:  int = 0,
    show_pdf: int = 0,
    sort:     str = "",
    dir:      str = "desc",
    msg:      str = "",
    msg_type: str = "success"
):
    redir, user = require_login(session)
    if redir: return redir

    today      = datetime.now().strftime("%Y-%m-%d")
    is_prop    = is_property_ledger(ledger)
    table      = "property_invoices" if is_prop else "supplier_invoices"
    loc_col    = "property_name"     if is_prop else "store_name"
    loc_val    = prop_name(ledger)   if is_prop else ledger
    ledgers    = ledger_options(user)
    PAGE_SIZE  = 30

    # If edit_id given, load that invoice into the form
    edit_inv = None
    if edit_id:
        rows = q(f"SELECT * FROM {table} WHERE invoice_id=?", (edit_id,), fetch=True)
        if rows:
            edit_inv = dict(rows[0])

    invoices, total_n = fetch_invoices(ledger, search, status, pg, PAGE_SIZE, sort, dir)
    total_pages = max(1, (total_n + PAGE_SIZE - 1) // PAGE_SIZE)

    # Pending approvals count (managers/owners only)
    pending_count = 0
    if user["role"] in ("owner", "manager"):
        p1 = q(f"SELECT COUNT(*) as n FROM {table} WHERE {loc_col}=? AND approval_status='pending'",
               (loc_val,), fetch=True)
        pending_count = p1[0]["n"] if p1 else 0

    # Summary totals for this ledger. "Paid (YTD)" counts only payments dated in
    # the current calendar year (paid_date on/after 1 Jan) — change year_start to
    # your accounting-year start if you'd rather it run from e.g. 6 April.
    year_start = datetime.now().strftime("%Y-01-01")
    year_label = datetime.now().strftime("%Y")
    tots = q(f"""
        SELECT
          COUNT(*) as total_count,
          COALESCE(SUM(CASE WHEN is_paid!='Yes' AND due_date < '{today}' AND COALESCE(payment_method,'')!='Direct Debit' THEN gross_amount-amount_paid-credit_note ELSE 0 END),0) as overdue_val,
          COUNT(CASE WHEN is_paid!='Yes' AND due_date < '{today}' AND COALESCE(payment_method,'')!='Direct Debit' THEN 1 END) as overdue_count,
          COALESCE(SUM(CASE WHEN is_paid='Yes' AND paid_date >= '{year_start}' THEN amount_paid ELSE 0 END),0) as paid_val
        FROM {table} WHERE {loc_col}=?
    """, (loc_val,), fetch=True)
    t = dict(tots[0]) if tots else {}

    # ── Flash message ──
    flash = ""
    if msg:
        cls = "flash-success" if msg_type == "success" else "flash-error"
        flash = f"<div class='{cls}'>{msg}</div>"

    # ── Ledger selector ──
    ledger_opts = ""
    for val, label in ledgers:
        sel = "selected" if val == ledger else ""
        ledger_opts += f"<option value='{val}' {sel}>{label}</option>"

    # ── Summary bar ──
    summary = f"""
    <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(160px,1fr))'>
      <div class='card py-3 text-center'>
        <div class='text-xs font-bold text-slate-400 uppercase'>Total Invoices</div>
        <div class='text-2xl font-black text-slate-800'>{t.get('total_count',0)}</div>
      </div>
      <div class='card py-3 text-center'>
        <div class='text-xs font-bold text-slate-400 uppercase'>Overdue</div>
        <div class='text-2xl font-black text-rose-600'>{t.get('overdue_count',0)}</div>
        <div class='text-xs text-rose-400 mono'>£{t.get('overdue_val',0):,.2f}</div>
      </div>
      <div class='card py-3 text-center'>
        <div class='text-xs font-bold text-slate-400 uppercase'>Total Paid (YTD {year_label})</div>
        <div class='text-2xl font-black text-emerald-600'>£{t.get('paid_val',0):,.2f}</div>
      </div>
    </div>"""

    # ── Search & filter bar ──
    status_opts = ""
    for val, label in [("","All"),("overdue","Overdue"),("dd_reconcile","🏦 DD to reconcile"),("unpaid","Unpaid"),
                        ("partial","Partial"),("paid","Paid"),
                        ("awaiting","⏳ Awaiting VAT invoice"),
                        ("credits","💳 Available credit notes"),
                        ("query","❓ Under query")]:
        sel = "selected" if val == status else ""
        status_opts += f"<option value='{val}' {sel}>{label}</option>"

    search_bar = f"""
    <div class='card'>
      <form method='GET' action='/invoices' class='flex flex-wrap gap-3 items-end'>
        <input type='hidden' name='ledger' value='{ledger}'>
        <div style='flex:2;min-width:200px'>
          <label>Search supplier, invoice no., serial no. or demand ref</label>
          <input type='text' name='search' value='{search}'
            placeholder='e.g. Bestway, INV-001, 42, KWAP2/30453...'>
        </div>
        <div style='min-width:130px'>
          <label>Status</label>
          <select name='status'>{status_opts}</select>
        </div>
        <div style='display:flex;gap:8px;align-items:flex-end'>
          <button type='submit' class='btn-primary'>🔍 Search</button>
          <a href='/invoices?ledger={ledger}' class='btn-secondary'>✕ Clear</a>
        </div>
      </form>
    </div>"""

    # ── Add / Edit form ──
    inv    = edit_inv or {}
    is_edit = bool(edit_inv)
    form_action = f"/invoices/save/{edit_id}" if is_edit else "/invoices/save/0"
    form_title  = f"✏️ Edit Invoice — {inv.get('supplier_name','')} {inv.get('invoice_number','')}" if is_edit else "➕ New Invoice"
    cancel_url  = f"/invoices?ledger={ledger}"
    # Staff can't edit the auto-calculated fields (VAT, Net, Due, Terms); owner/manager can.
    lock_calc   = (user.get("role") == "staff")
    # Supplier -> term rule map for auto-filling the due date on the form.
    import json
    _terms_rows = q("SELECT supplier_name, term_type, term_value FROM supplier_terms WHERE term_type IS NOT NULL",
                    (), fetch=True) or []
    supplier_terms_js = json.dumps({r["supplier_name"]: {"t": r["term_type"], "v": r["term_value"]}
                                    for r in _terms_rows})
    # Supplier -> VAT reclaim % (only those with a non-default %, e.g. Alphabet=50).
    _vat_rows = q("SELECT supplier_name, vat_reclaim_pct FROM supplier_terms WHERE vat_reclaim_pct IS NOT NULL",
                  (), fetch=True) or []
    supplier_vat_js = json.dumps({r["supplier_name"]: r["vat_reclaim_pct"] for r in _vat_rows})
    # Existing supplier names for the Supplier field's autocomplete (cuts down
    # duplicates/typos by suggesting names already on record).
    _sup_rows = q("""SELECT DISTINCT supplier_name s FROM (
                       SELECT supplier_name FROM supplier_invoices
                       UNION SELECT supplier_name FROM property_invoices)
                     WHERE supplier_name IS NOT NULL AND supplier_name!='' ORDER BY supplier_name""",
                  (), fetch=True) or []
    supplier_datalist = ("<datalist id='supplierlist'>"
                         + "".join(f"<option value=\"{r['s'].replace(chr(34), '&quot;')}\">" for r in _sup_rows)
                         + "</datalist>")

    def fi(name, label, ftype="text", val=None, req=False, opts=None, placeholder="",
           lock=False, hi=False, calc=False, dlist=""):
        """Render a form field. Colours match the form's key:
        req  = HTML-required + red * (must have, e.g. Supplier).
        hi   = amber accent = a key 'please enter by hand' field.
        calc = green tint = auto-calculated field (VAT/Net/Due/Terms).
        lock = also read-only (staff can't edit the calc fields; owner can).
        """
        safe_val = val if val is not None else ""
        req_attr = "required" if req else ""
        # step='any' avoids over-strict browser validation when a stored value
        # carries repeating decimals (e.g. VAT = gross / 6 = 1902.56166666…).
        step     = "step='any'" if ftype == "number" else ""
        ph       = f"placeholder='{placeholder}'" if placeholder else ""
        mark     = " <span style='color:#dc2626'>*</span>" if req else ""
        styles   = []
        if lock:
            # Read-only for staff: show plainly greyed/disabled (NOT the green auto-calc
            # tint) so staff clearly see "you can see this, but you can't edit it".
            styles.append("background:#f1f5f9;color:#94a3b8;cursor:not-allowed;border-left:4px solid #cbd5e1")
        elif calc:
            styles.append("background:#f0fdf4;border-left:4px solid #86efac")   # green = auto-calculated
        elif req or hi:
            styles.append("border-left:4px solid #f59e0b")                      # amber = please enter
        style_attr = f"style=\"{';'.join(styles)}\"" if styles else ""
        ro = "readonly" if lock else ""
        if opts is not None:
            o_html = ""
            for ov, ol in opts:
                sel = "selected" if str(safe_val) == str(ov) else ""
                o_html += f"<option value='{html.escape(str(ov), quote=True)}' {sel}>{html.escape(str(ol))}</option>"
            dis = "disabled" if lock else ""
            return f"<div><label>{label}{mark}</label><select name='{name}' {req_attr} {dis} {style_attr}>{o_html}</select></div>"
        list_attr = f"list='{dlist}' autocomplete='off'" if dlist else ""
        return f"<div><label>{label}{mark}</label><input type='{ftype}' name='{name}' value='{html.escape(str(safe_val), quote=True)}' {req_attr} {step} {ph} {ro} {list_attr} {style_attr}></div>"

    # Payment status fields (only show if editing)
    payment_fields = ""
    if is_edit:
        paid_opts  = [("No","Unpaid"),("Yes","Paid")]
        meth_opts  = [(m, m or "-- Select --") for m in PAYMENT_METHODS]
        balance    = (inv.get("gross_amount") or 0) - (inv.get("amount_paid") or 0) - (inv.get("credit_note") or 0)
        # Cheque Number (both retail and property) shows only when method=Cheque.
        # DD Statement Date is retail-only (property invoices have no DD workflow).
        pay_lock = "readonly style=\"background:#f0fdf4;color:#64748b;cursor:not-allowed\"" if lock_calc else ""
        cheque_field = (
            "<div id='chequeWrap' style=\""
            + ("" if (inv.get('payment_method') or '') == 'Cheque' else "display:none")
            + "\"><label>Cheque Number</label>"
            + f"<input type='text' name='cheque_number' value='{inv.get('cheque_number') or ''}' {pay_lock}></div>")
        if is_prop:
            dd_cheque_fields = cheque_field
        else:
            dd_cheque_fields = (
                fi('dd_statement_date', 'DD Statement Date', 'date', inv.get('dd_statement_date',''), lock=lock_calc)
                + cheque_field)
        staff_note = ("<div style='font-size:11px;color:#94a3b8;margin-bottom:6px'>"
                      "🔒 Payment details are managed by the owner — shown here for information only.</div>"
                      if lock_calc else "")
        payment_fields = f"""
        <div class='col-span-2' style='border-top:1px solid #e2e8f0;padding-top:12px;margin-top:4px'>
          <div class='text-xs font-bold text-slate-500 uppercase tracking-wide mb-3'>Payment Details</div>
          {staff_note}
          <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr))'>
            {fi('is_paid',        'Status',          opts=paid_opts,  val=inv.get('is_paid','No'), lock=lock_calc)}
            {fi('paid_date',      'Paid Date',        'date',          inv.get('paid_date',''), lock=lock_calc)}
            {fi('payment_method', 'Payment Method',   opts=meth_opts,  val=inv.get('payment_method',''), lock=lock_calc)}
            {fi('amount_paid',    'Amount Paid (£)',  'number',        inv.get('amount_paid',0), lock=lock_calc)}
            {fi('credit_note',    'Credit Note (£)',  'number',        inv.get('credit_note',0), lock=lock_calc)}
            {dd_cheque_fields}
          </div>
          <div class='text-xs text-slate-400 mt-2 mono'>
            Balance outstanding: <strong class='{'text-rose-600' if balance > 0 else 'text-emerald-600'}'>£{balance:,.2f}</strong>
          </div>
        </div>"""

    # Property-specific field
    prop_or_store_field = ""
    if is_prop:
        prop_or_store_field = fi('expense_type', 'Expense Type',
            opts=[(e, e or "-- Select --") for e in [""] + EXPENSE_TYPES],
            val=inv.get('expense_type',''))
    
    # Serial number — retail and property each keep their OWN shared sequence
    # (retail: one run across both stores; property: one run across all
    # properties incl. MREL). Next number = highest in that table + 1.
    seq_default = inv.get('seq_no', '')
    if not is_edit:
        mx  = q(f"SELECT MAX(seq_no) AS m FROM {table}", (), fetch=True)
        seq_default = ((dict(mx[0]).get('m') or 0) + 1) if mx else 1
    seq_field = fi('seq_no', 'Serial No.', 'number', seq_default)

    # ── Freed serial numbers available to reuse (gaps left by deleted invoices).
    #    Owner only — store staff (and managers) just take the next number,
    #    keeping their screen simple and avoiding confusion. ──
    freed_html = ""
    if (not is_edit) and user.get("role") == "owner":
        present = sorted({int(r["seq_no"]) for r in
                          q(f"SELECT seq_no FROM {table} WHERE seq_no IS NOT NULL",
                            (), fetch=True)})
        if present:
            ps = set(present)
            gaps = [n for n in range(present[0], present[-1]) if n not in ps]
            if gaps:
                chips = "".join(
                    f"<button type='button' onclick=\"document.querySelector('[name=seq_no]').value={g}\" "
                    f"style='background:#ecfdf5;border:1px solid #6ee7b7;color:#047857;border-radius:6px;"
                    f"padding:2px 8px;margin:2px;font-size:12px;font-weight:700;cursor:pointer'>{g}</button>"
                    for g in gaps[:40])
                more = f" <span style='color:#94a3b8;font-size:12px'>+{len(gaps)-40} more</span>" if len(gaps) > 40 else ""
                freed_html = (
                    "<div style='grid-column:1/-1;background:#f0fdf4;border:1px solid #bbf7d0;"
                    "border-radius:8px;padding:8px 12px;margin-bottom:4px'>"
                    "<span style='font-size:12px;font-weight:700;color:#047857'>♻️ Freed numbers available to reuse "
                    "<span style='font-weight:400;color:#64748b'>(click to use, or ignore to take the next number)</span>:</span> "
                    f"{chips}{more}</div>")

    # ── Owner-only "Sent to Accountant" date (edit mode, retail or property) ──
    accountant_field = ""
    if is_edit and user.get("role") == "owner":
        accountant_field = fi('accountant_sent_date', 'Sent to Accountant',
                              'date', inv.get('accountant_sent_date', ''))

    # ── Owner-only "Linked Invoice / CN Ref" — links a credit note to the invoice
    #    it offsets (and vice-versa). A credit note that has this is "applied". ──
    linked_field = ""
    if user.get("role") == "owner":
        linked_field = fi('linked_ref', 'Linked Invoice / CN Ref',
                          val=inv.get('linked_ref', ''),
                          placeholder='e.g. the invoice or CN this relates to')

    # ── Demand note / pro-forma handling ──
    # "Awaiting VAT invoice" is now AUTOMATIC: shown when a demand/pro-forma ref
    # is entered with no invoice number (so staff can't tick it in error). The
    # indicator below is read-only and toggled live by JS; owner/manager get an
    # override tick for the rare case with no demand ref.
    demand_field = fi('demand_ref', 'Demand / Pro-forma Ref', val=inv.get('demand_ref', ''))
    _await_now = bool((inv.get('demand_ref') or '').strip()) and not (inv.get('invoice_number') or '').strip()
    _await_shown = _await_now or (inv.get('awaiting_invoice') == 'Yes')
    override_html = ""
    if user.get("role") in ("owner", "manager"):
        _forced = (inv.get('awaiting_invoice') == 'Yes') and not _await_now
        override_html = (
            "<div style='grid-column:1/-1;font-size:12px;color:#64748b;margin-top:-4px'>"
            "<label style='display:flex;align-items:center;gap:6px'>"
            f"<input type='checkbox' name='awaiting_override' value='Yes' {'checked' if _forced else ''}>"
            "Owner override: force “awaiting VAT invoice” (for a rare case with no demand ref)</label></div>")
    awaiting_field = (
        "<div id='awaitingBox' style='grid-column:1/-1;background:#fffbeb;border:1px solid #fde68a;"
        "border-radius:8px;padding:8px 12px;" + ("" if _await_shown else "display:none") + "'>"
        "<span style='font-size:13px;font-weight:700;color:#92400e'>⏳ Awaiting VAT invoice</span> "
        "<span style='font-size:12px;color:#a16207'>— treated as a demand note / pro-forma (a reference is "
        "entered with no invoice number). VAT is held aside until the invoice number is entered.</span>"
        "</div>" + override_html)

    # ── "Under query" flag (staff can set/clear it — they raise the queries).
    #    Details go in the Comments box. ──
    _uq = (inv.get('under_query') == 'Yes')
    query_field = (
        "<div style='grid-column:1/-1;background:" + ("#fef2f2" if _uq else "#f8fafc") + ";"
        "border:1px solid #fecaca;border-radius:8px;padding:8px 12px'>"
        "<label style='display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;color:#b91c1c'>"
        f"<input type='checkbox' name='under_query' value='Yes' {'checked' if _uq else ''}>"
        "❓ Under query <span style='font-weight:400;color:#7f1d1d'>— item missing/damaged, or being "
        "queried with the supplier. Put the details in Comments below. Untick when resolved.</span></label></div>")

    # Thumbnail preview of the attached PDF (edit mode, when a file is attached)
    pdf_preview = ""
    if is_edit and inv.get("pdf_path"):
        # cache-buster (?v=file-mtime): the preview re-fetches whenever the PDF file
        # changes (e.g. after removing a page) instead of showing a stale cached copy
        try:
            _v = f"&v={int(os.path.getmtime(inv['pdf_path']))}"
        except Exception:
            _v = ""
        thumb_url = f"/invoices/pdf-thumb/{edit_id}?ledger={ledger}{_v}"
        full_url  = f"/invoices/pdf/{edit_id}?ledger={ledger}{_v}"
        # Count pages so we can offer to remove a stray page (e.g. a store report
        # scanned in with the invoice) without deleting and re-attaching the whole PDF.
        page_count = 0
        try:
            _pp = inv["pdf_path"]
            if _pp and _pp.lower().endswith(".pdf") and os.path.exists(_pp):
                from pypdf import PdfReader
                page_count = len(PdfReader(_pp).pages)
        except Exception:
            page_count = 0
        remove_page_html = ""
        if page_count > 1:
            _opts = "".join(f"<option value='{n}'>Page {n}</option>" for n in range(1, page_count + 1))
            remove_page_html = (
                f"<form method='POST' action='/invoices/pdf/{edit_id}/remove-page' "
                f"onsubmit=\"return confirm('Remove the selected page from this invoice PDF? "
                f"A backup of the original is kept.');\" "
                f"style='margin-top:8px;display:flex;gap:6px;align-items:center;flex-wrap:wrap'>"
                f"<input type='hidden' name='ledger' value='{ledger}'>"
                f"<span style='font-size:12px;color:#b45309;font-weight:700'>PDF has {page_count} pages —</span>"
                f"<select name='page' style='font-size:12px;padding:2px 6px'>{_opts}</select>"
                f"<button type='submit' class='btn-secondary' style='padding:3px 10px;font-size:11px'>"
                f"&#128465;&#65039; Remove page</button></form>")
        pdf_preview = f"""
        <div style='margin-top:12px;display:flex;gap:14px;align-items:flex-start'>
          <img src='{thumb_url}' alt='Invoice preview'
               onclick="showPdf('{full_url}')"
               style='width:96px;height:auto;border:1px solid #cbd5e1;border-radius:8px;
                      cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.10)'>
          <div style='font-size:12px;color:#475569'>
            <div style='font-weight:700;color:#0369a1;margin-bottom:4px'>&#9989; Invoice PDF attached</div>
            <a href='#' onclick="showPdf('{full_url}');return false;"
               style='color:#1e3a5f;font-weight:700'>&#128065;&#65039; View full invoice</a>
            <div style='color:#94a3b8;margin-top:4px'>
              Click the thumbnail to enlarge. To replace it, choose a new file above.
            </div>
            {remove_page_html}
          </div>
        </div>"""
        # Opened from a report with &show_pdf=1 → auto-open the side-by-side PDF.
        if show_pdf:
            pdf_preview += f"<script>window.addEventListener('DOMContentLoaded',function(){{showPdf('{full_url}');}});</script>"

    # ── Audit line (edit mode): who entered it / who last edited it, and when ──
    audit_html = ""
    if is_edit:
        bits = [f"Entered by <b>{inv.get('submitted_by') or '—'}</b> on {fmt_uk_dt(inv.get('created_at'))}"]
        if inv.get("updated_at"):
            bits.append(f"last edited by <b>{inv.get('updated_by') or '—'}</b> on {fmt_uk_dt(inv.get('updated_at'))}")
        audit_html = ("<div style='font-size:11px;color:#94a3b8;margin-bottom:10px'>🕒 "
                      + " &nbsp;·&nbsp; ".join(bits) + "</div>")

    # PDF attach behaves differently when editing: on an EXISTING invoice, attaching a
    # PDF must NOT re-extract/overwrite the fields already entered — it only saves the
    # document. Auto-fill stays on for NEW invoices (where it's a typing aid).
    _pdf_hint = ("— attaches the PDF and shows it side-by-side; your entered details are left unchanged"
                 if is_edit else "— uploads once, auto-fills fields AND saves the PDF with the record")
    _pdf_onchange = "previewPdfOnly()" if is_edit else "extractPdf()"
    _pdf_remove_btn = ("" if is_edit else
        "<button type='button' id='pdf_remove' onclick='removePdf()' "
        "style='display:none;background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;"
        "border-radius:6px;padding:4px 10px;font-size:12px;font-weight:700;cursor:pointer'>"
        "✕ Remove / change PDF</button>")
    _pdf_note = ("The PDF is saved with this invoice when you press <b>Update</b>. "
                 "<b>Attaching it does not change your entered details.</b>"
                 if is_edit else
                 "Fields auto-fill from the PDF where possible. "
                 "<span style='background:#f0fdf4;border:1px solid #86efac;border-radius:4px;padding:1px 5px'>green = auto-filled</span> "
                 "<span style='background:#fffbeb;border:1px solid #f59e0b;border-radius:4px;padding:1px 5px'>amber = please check / enter by hand</span> "
                 "Always check before saving.")
    form_html = f"""
    <div class='card' id='invoice-form'>
      {audit_html}
      <!-- PDF Upload — one file does both: auto-fills fields AND saves with invoice -->
      <div style='background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:12px 16px;margin-bottom:16px'>
        <div style='font-size:13px;font-weight:700;color:#0369a1;margin-bottom:8px'>
          📎 Attach Invoice PDF
          <span style='font-weight:400;color:#64748b;font-size:12px;margin-left:8px'>
            {_pdf_hint}
          </span>
        </div>
        <div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap'>
          <input type='file' name='pdf_file' id='pdf_prefill' accept='.pdf'
            form='invoiceForm' onchange="{_pdf_onchange}"
            style='flex:1;min-width:200px;border:1px solid #bae6fd;background:white;padding:5px 10px;border-radius:8px;font-size:13px'>
          <span id='pdf_status' style='font-size:12px;color:#0369a1'></span>
          {_pdf_remove_btn}
        </div>
        <div style='font-size:11px;color:#94a3b8;margin-top:6px'>
          {_pdf_note}
        </div>
        {pdf_preview}
      </div>
      <div class='flex justify-between items-center mb-4'>
        <div class='font-black text-slate-800'>{form_title}</div>
        {'<a href="' + cancel_url + '" class="btn-secondary text-xs">✕ Cancel Edit</a>' if is_edit else ''}
      </div>
      <form id='invoiceForm' action='{form_action}' method='POST' enctype='multipart/form-data'>
        <input type='hidden' name='ledger' value='{ledger}'>
        <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(180px,1fr))'>
          {freed_html}
          {seq_field}
          {(fi('supplier_name', 'Supplier Name', val=inv.get('supplier_name',''), req=True, opts=[('', '— select a supplier —')] + [(r['s'], r['s']) for r in _sup_rows]) if user.get('role') == 'staff' else fi('supplier_name', 'Supplier Name', val=inv.get('supplier_name',''), req=True, dlist='supplierlist') + supplier_datalist)}
          {fi('invoice_number', 'Invoice Number',   val=inv.get('invoice_number',''), hi=True)}
          {demand_field}
          {fi('invoice_date',   'Invoice Date',     'date', inv.get('invoice_date',''), hi=True)}
          {fi('due_date',       'Due Date',         'date', inv.get('due_date',''), lock=lock_calc, calc=True)}
          {fi('gross_amount',   'Gross Amount (£)', 'number', inv.get('gross_amount',0), hi=True)}
          {fi('vat_amount',     'VAT Amount (£)',   'number', inv.get('vat_amount',0), lock=lock_calc, calc=True)}
          {fi('net_amount',     'Net Amount (£)',   'number', inv.get('net_amount',0), lock=lock_calc, calc=True)}
          {fi('claimable_vat',  'Claimable VAT (£)','number', inv.get('claimable_vat',''), lock=lock_calc, calc=True)}
          <div id='vatnote' style='grid-column:1/-1;font-size:12px;color:#0369a1;font-weight:700;margin-top:-4px'></div>
          {'' if is_prop else fi('payment_terms', 'Terms (days)', 'number', inv.get('payment_terms',''), lock=lock_calc, calc=True)}
          {prop_or_store_field}
          {accountant_field}
          {linked_field}
          {awaiting_field}
          {query_field}
          <!-- PDF attached via the strip above -->
          <div style='grid-column:1/-1'>
            {fi('comments','Comments', val=inv.get('comments',''))}
          </div>
          {payment_fields}
        </div>
        <div class='flex gap-3 mt-4 items-center'>
          <button type='submit' class='btn-primary'>{'💾 Update Invoice' if is_edit else '➕ Save Invoice'}</button>
          {'<a href="/invoices/delete/' + str(edit_id) + '?ledger=' + ledger + '" class="btn-danger" onclick=\"return confirm(\'Delete this invoice?\');\">🗑️ Delete</a>' if (is_edit and user.get('role') == 'owner') else ''}
          <a href='{cancel_url}' class='btn-secondary'>Cancel</a>
          {"<label style='display:flex;align-items:center;gap:6px;font-size:13px;color:#475569;margin-left:8px'><input type='checkbox' name='save_pending' value='1' " + ('checked' if inv.get('approval_status')=='pending' else '') + "> Mark as pending (review later)</label>" if user.get('role') in ('owner','manager') else ''}
        </div>
      </form>
    </div>"""

    # ── Invoice list ──
    rows_html = ""
    for row in invoices:
        paid    = row["amount_paid"]  or 0
        credit  = row["credit_note"]  or 0 if not is_prop else 0
        balance = row["balance"]      or 0
        today_s = datetime.now().strftime("%Y-%m-%d")

        approval = row.get("approval_status", "approved")
        if approval == "pending":
            badge = "<span style='background:#fef3c7;color:#92400e;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px'>⏳ PENDING</span>"
            row_cls = "style='background:#fffbeb'"
        elif row["is_paid"] == "Yes":
            badge = "<span class='badge-paid'>PAID</span>"
            row_cls = ""
        elif row["due_date"] and row["due_date"] < today_s:
            if (row.get("payment_method") or "") == "Direct Debit":
                # DD collects automatically on/before the due date — show it as awaiting
                # the owner's statement reconciliation, not as a red "OVERDUE".
                badge = ("<span style='background:#e0f2fe;color:#075985;font-size:11px;font-weight:700;"
                         "padding:2px 8px;border-radius:6px'>🏦 DD · to reconcile</span>")
                row_cls = ""
            else:
                badge = "<span class='badge-overdue'>OVERDUE</span>"
                row_cls = "style='background:#fff5f5'"
        elif paid > 0:
            badge = "<span class='badge-partial'>PARTIAL</span>"
            row_cls = "style='background:#fffbeb'"
        else:
            badge = "<span class='badge-unpaid'>UNPAID</span>"
            row_cls = ""

        # Awaiting-VAT-invoice (demand note / pro-forma) is independent of payment
        # status, so it shows as an extra tag above the payment badge.
        if row.get("awaiting_invoice") == "Yes":
            badge = ("<span style='background:#fef3c7;color:#92400e;font-size:10px;font-weight:700;"
                     "padding:1px 6px;border-radius:6px;display:inline-block;margin-bottom:3px'>"
                     "⏳ AWAITING VAT INV</span><br>" + badge)
            if not row_cls:
                row_cls = "style='background:#fffbeb'"

        # Credit notes / refunds (negative gross) show red, like the Excel sheet.
        _g = row['gross_amount'] or 0
        gross_str = (f"-£{abs(_g):,.2f}" if _g < 0 else f"£{_g:,.2f}")
        gross_col = "#dc2626" if _g < 0 else "#0f172a"
        # For a credit note, the status badge shows whether it's been applied
        # (linked to an invoice) or is still an available credit.
        if _g < 0:
            if (row.get('linked_ref') or '').strip():
                badge = ("<span style='background:#dcfce7;color:#16a34a;font-size:11px;font-weight:700;"
                         "padding:2px 8px;border-radius:6px'>✔ CN APPLIED</span>")
            else:
                badge = ("<span style='background:#dbeafe;color:#1e40af;font-size:11px;font-weight:700;"
                         "padding:2px 8px;border-radius:6px'>CREDIT · AVAILABLE</span>")

        # "Under query" tag sits above whatever the payment/credit badge is.
        if row.get("under_query") == "Yes":
            badge = ("<span style='background:#fee2e2;color:#b91c1c;font-size:10px;font-weight:700;"
                     "padding:1px 6px;border-radius:6px;display:inline-block;margin-bottom:3px'>"
                     "❓ UNDER QUERY</span><br>" + badge)
            if not row_cls:
                row_cls = "style='background:#fef2f2'"

        seq_td = f"<td class='mono' style='color:#94a3b8;font-size:11px'>{row['seq_no'] or ''}</td>"
        pdf_td = ""
        if row.get("pdf_path"):
            pdf_url = f"/invoices/pdf/{row['invoice_id']}?ledger={ledger}"
            pdf_td  = (f'<a href="#" onclick="event.stopPropagation();showPdf(\'{pdf_url}\');return false;" '
                       f'style="color:#1e3a5f;font-size:11px;font-weight:700">&#128206; View</a>')

        # Approve/reject buttons for pending invoices (managers/owners only)
        approval_td = ""
        row_approval = row.get("approval_status", "approved")
        if row_approval == "pending" and user["role"] in ("owner","manager"):
            approval_td = f"""
            <a href='/invoices/approve/{row['invoice_id']}?ledger={ledger}'
               style='background:#dcfce7;color:#16a34a;font-size:11px;font-weight:700;
                      padding:3px 8px;border-radius:6px;text-decoration:none;margin-right:4px'
               onclick='event.stopPropagation()'>✅ Approve</a>
            <a href='/invoices/reject/{row['invoice_id']}?ledger={ledger}'
               style='background:#fee2e2;color:#dc2626;font-size:11px;font-weight:700;
                      padding:3px 8px;border-radius:6px;text-decoration:none'
               onclick='event.stopPropagation()'
               onclick="return confirm('Reject this invoice?')">❌ Reject</a>"""

        rows_html += f"""
        <tr {row_cls} onclick="selectInvoice({row['invoice_id']}, '{ledger}')"
            style='cursor:pointer' id='row-{row['invoice_id']}'>
          {seq_td}
          <td style='font-weight:700;color:#0f172a'>{row['supplier_name']}</td>
          <td class='mono' style='font-size:12px'>{row['invoice_number'] or '—'}</td>
          <td class='mono' style='font-size:12px;color:#64748b'>{fmt_uk_date(row['invoice_date'])}</td>
          <td class='mono' style='font-size:12px;color:#64748b'>{fmt_uk_date(row['due_date'])}</td>
          <td class='mono' style='font-weight:700;color:{gross_col}'>{gross_str}</td>
          <td class='mono' style='color:#16a34a'>{'£'+f'{paid:,.2f}' if paid else '—'}</td>
          <td class='mono' style='font-weight:700;color:{"#dc2626" if balance > 0 else "#16a34a"}'>£{balance:,.2f}</td>
          <td>{badge}</td>
          <td style='font-size:12px;color:#64748b'>{row['payment_method'] or '—'}</td>
          <td>{pdf_td}</td>
          <td>{approval_td}</td>
        </tr>"""

    # Clickable, server-side sortable column headers. Clicking toggles
    # asc/desc; a ▲/▼ marks the active column.
    def sort_th(label, key):
        active = (sort == key)
        nxt   = "asc" if (active and dir == "desc") else ("desc" if active else "asc")
        arrow = " ▼" if (active and dir == "desc") else (" ▲" if active else "")
        qs = f"/invoices?ledger={urlquote(ledger)}&sort={key}&dir={nxt}"
        if search: qs += f"&search={urlquote(search)}"
        if status: qs += f"&status={urlquote(status)}"
        qs += "#list"
        return (f"<th style='cursor:pointer;white-space:nowrap'>"
                f"<a href='{qs}' style='color:inherit;text-decoration:none'>{label}{arrow}</a></th>")

    seq_th = sort_th("Serial", "seq")
    headers_html = (
        sort_th("Supplier", "supplier") + sort_th("Invoice No.", "invno")
        + sort_th("Inv. Date", "invdate") + sort_th("Due Date", "due")
        + sort_th("Gross", "gross") + "<th>Paid</th>"
        + sort_th("Balance", "balance") + sort_th("Status", "status")
        + "<th>Method</th><th>PDF</th>"
    )

    # Pagination
    pag_html = ""
    if total_pages > 1:
        base = f"/invoices?ledger={urlquote(ledger)}&search={urlquote(search)}&status={status}&sort={sort}&dir={dir}&pg="
        pag_html = "<div class='flex gap-2 flex-wrap justify-center'>"
        for p in range(1, total_pages + 1):
            cls = "btn-primary" if p == pg else "btn-secondary"
            pag_html += f"<a href='{base}{p}#list' class='{cls}' style='padding:6px 14px'>{p}</a>"
        pag_html += "</div>"

    reset_link = (f"<a href='/invoices?ledger={urlquote(ledger)}#list' "
                  f"style='color:#fbbf24;font-size:12px;text-decoration:underline;margin-right:14px'>"
                  f"↺ Default view (newest first)</a>") if sort else ""
    list_html = f"""
    <div class='card' id='list' style='padding:0;overflow:hidden'>
      <div style='padding:16px 20px;background:#0f2942;display:flex;justify-content:space-between;align-items:center'>
        <div style='color:white;font-weight:700;font-size:14px'>
          {total_n} invoices
          {'· <span style="color:#fbbf24">'+str(t.get('overdue_count',0))+' overdue</span>' if t.get('overdue_count',0) > 0 else ''}
        </div>
        <div style='display:flex;align-items:center'>
          {reset_link}<span style='color:#93c5fd;font-size:12px'>Click a column title to sort · click any row to edit</span>
        </div>
      </div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead>
            <tr>
              {seq_th}{headers_html}
            </tr>
          </thead>
          <tbody>{rows_html if rows_html else "<tr><td colspan='10' style='text-align:center;padding:32px;color:#94a3b8'>No invoices found</td></tr>"}</tbody>
        </table>
      </div>
    </div>
    {pag_html}"""

    # ── JS: click row to scroll to form and load edit ──
    js = """
    <script>
    function selectInvoice(id, ledger) {
      document.querySelectorAll('.tbl tbody tr').forEach(r => r.style.outline = '');
      const row = document.getElementById('row-' + id);
      if (row) row.style.outline = '2px solid #1e3a5f';
      window.location.href = '/invoices?ledger=' + ledger + '&edit_id=' + id + '#invoice-form';
    }

    // ── Smart field calculations ──
    document.addEventListener('DOMContentLoaded', function() {
      const gross = document.querySelector('[name="gross_amount"]');
      const vat   = document.querySelector('[name="vat_amount"]');
      const net   = document.querySelector('[name="net_amount"]');
      const idate = document.querySelector('[name="invoice_date"]');
      const ddate = document.querySelector('[name="due_date"]');
      const terms = document.querySelector('[name="payment_terms"]');

      // Show the Cheque Number box only when the payment method is Cheque.
      const pmeth = document.querySelector('[name="payment_method"]');
      const chequeWrap = document.getElementById('chequeWrap');
      function toggleCheque() {
        if (chequeWrap) chequeWrap.style.display = (pmeth && pmeth.value === 'Cheque') ? '' : 'none';
      }
      if (pmeth) pmeth.addEventListener('change', toggleCheque);
      toggleCheque();

      // Live "Awaiting VAT invoice" indicator: a demand/pro-forma ref with no
      // invoice number (or the owner override). No manual tick to get wrong.
      const dref = document.querySelector('[name="demand_ref"]');
      const invno = document.querySelector('[name="invoice_number"]');
      const awBox = document.getElementById('awaitingBox');
      const awOv = document.querySelector('[name="awaiting_override"]');
      function toggleAwaiting() {
        if (!awBox) return;
        const auto = dref && dref.value.trim() && (!invno || !invno.value.trim());
        const forced = awOv && awOv.checked;
        awBox.style.display = (auto || forced) ? '' : 'none';
      }
      if (dref)  dref.addEventListener('input', toggleAwaiting);
      if (invno) invno.addEventListener('input', toggleAwaiting);
      if (awOv)  awOv.addEventListener('change', toggleAwaiting);
      toggleAwaiting();

      // Auto-calc net = gross - vat
      function recalcNet() {
        if (gross && vat && net) {
          const g = parseFloat(gross.value) || 0;
          const v = parseFloat(vat.value)   || 0;
          // Only auto-fill net if it's empty or was previously auto-filled
          if (!net.dataset.manual) net.value = (g - v).toFixed(2);
        }
      }
      // Auto-calc VAT = gross * 1/6 (standard 20% VAT on gross)
      function recalcVat() {
        if (gross && vat && !vat.dataset.manual) {
          const g = parseFloat(gross.value) || 0;
          vat.value = (g / 6).toFixed(2);
          recalcNet();
        }
      }
      // Auto-calc due date from invoice date + terms
      function recalcDueDate() {
        if (idate && ddate && terms && idate.value && terms.value && !ddate.dataset.manual) {
          const d = new Date(idate.value);
          d.setDate(d.getDate() + parseInt(terms.value));
          ddate.value = d.toISOString().split('T')[0];
        }
      }

      if (gross) gross.addEventListener('input', recalcVat);
      if (vat)   { vat.addEventListener('input',   () => { vat.dataset.manual='1'; recalcNet(); }); }
      if (net)   { net.addEventListener('input',   () => { net.dataset.manual='1'; }); }
      if (idate) idate.addEventListener('change', recalcDueDate);
      if (terms) terms.addEventListener('input',  recalcDueDate);
      if (ddate) ddate.addEventListener('input',  () => { ddate.dataset.manual='1'; });
      // Keep invoice numbers uppercase — consistent and easy to scan when checking.
      // (reuse the `invno` declared above for the awaiting-VAT indicator)
      if (invno) invno.addEventListener('input', function() {
        const p = invno.selectionStart;
        invno.value = invno.value.toUpperCase();
        try { invno.setSelectionRange(p, p); } catch(e) {}
      });
      // Entering a Paid Date marks the invoice Paid automatically — a shortcut for
      // simple direct debits reconciled one-by-one against the bank statement.
      const pdate = document.querySelector('[name="paid_date"]');
      const pstat = document.querySelector('[name="is_paid"]');
      if (pdate && pstat) pdate.addEventListener('change', function() {
        if (pdate.value) pstat.value = 'Yes';
      });
    });

    // ── PDF auto-fill ──
    async function extractPdf() {
      const fileInput = document.getElementById('pdf_prefill');
      const status    = document.getElementById('pdf_status');
      if (!fileInput.files.length) return;
      // Show the PDF instantly (client-side) so you can read it while filling the form,
      // and reveal the "remove/change" button in case the wrong file was picked.
      try {
        if (window._pdfObjUrl) URL.revokeObjectURL(window._pdfObjUrl);
        window._pdfObjUrl = URL.createObjectURL(fileInput.files[0]);
        showPdf(window._pdfObjUrl);
        const rm = document.getElementById('pdf_remove');
        if (rm) rm.style.display = '';
      } catch(e) {}
      // File is already attached to the form — just extract the data
      status.textContent = '⏳ Reading PDF...';
      const formData = new FormData();
      formData.append('pdf_file', fileInput.files[0]);
      try {
        const resp = await fetch('/invoices/extract-pdf', { method:'POST', body:formData });
        const data = await resp.json();
        if (data.error) { status.textContent = '❌ ' + data.error; return; }

        // Fill fields if found in PDF
        const fill = (name, val) => {
          const el = document.querySelector('[name="' + name + '"]');
          if (el && val !== undefined && val !== null && val !== '') {
            el.value = val;
            if (!el.readOnly) el.style.background = '#f0fdf4';  // green tint (skip locked/staff fields)
          }
        };
        // Supplier is NOT auto-filled from the PDF: it's chosen from the dropdown of
        // existing suppliers instead, so PDF case/spelling quirks can't create duplicate
        // suppliers. It stays blank here and is flagged amber ("please enter") below.
        fill('invoice_number', (data.invoice_number || '').toUpperCase());
        fill('invoice_date',   data.invoice_date);
        fill('due_date',       data.due_date);
        fill('gross_amount',   data.gross_amount);
        fill('vat_amount',     data.vat_amount);
        fill('net_amount',     data.net_amount);
        fill('payment_terms',  data.payment_terms);

        // If the PDF gave us a due date, don't let the auto-recalc overwrite it.
        const dd = document.querySelector('[name="due_date"]');
        if (dd && data.due_date) dd.dataset.manual = '1';

        // Trigger calculations for any fields NOT found in PDF
        const gross = document.querySelector('[name="gross_amount"]');
        if (gross) gross.dispatchEvent(new Event('input'));
        const idate = document.querySelector('[name="invoice_date"]');
        if (idate) idate.dispatchEvent(new Event('change'));

        // ── Highlight anything the system could NOT fill, so staff don't
        //    assume it was completed correctly. Amber = please check/enter. ──
        const KEY_FIELDS = {
          supplier_name:'Supplier Name', invoice_number:'Invoice Number',
          invoice_date:'Invoice Date',   due_date:'Due Date',
          gross_amount:'Gross Amount',   vat_amount:'VAT Amount',
          net_amount:'Net Amount'
        };
        let missing = 0;
        for (const name of Object.keys(KEY_FIELDS)) {
          const el = document.querySelector('[name="' + name + '"]');
          if (!el || el.readOnly) continue;   // don't flag locked (staff) fields amber
          const v = (el.value || '').trim();
          const blank = v === '' || v === '0' || v === '0.00' || parseFloat(v) === 0;
          if (blank) {
            el.style.background = '#fffbeb';
            el.style.border     = '2px solid #f59e0b';
            el.dataset.needsInput = '1';
            missing++;
            // Clear the amber as soon as a staff member fills it in.
            el.addEventListener('input', function clr() {
              el.style.background = ''; el.style.border = '';
              el.dataset.needsInput = ''; el.removeEventListener('input', clr);
            });
          }
        }

        let found = Object.keys(data).filter(k => !k.startsWith('_') && k !== 'supplier_name' && data[k]).length;
        if (missing) {
          status.innerHTML = '✅ ' + found + ' fields auto-filled (green). ' +
            '<span style="color:#b45309;font-weight:700">' + missing +
            ' field(s) highlighted amber need to be entered/checked by hand.</span>';
          status.style.color = '#16a34a';
        } else {
          status.textContent = '✅ ' + found + ' fields auto-filled — please check before saving';
          status.style.color = '#16a34a';
        }

      } catch(e) {
        status.textContent = '❌ Could not read PDF — please fill manually';
        status.style.color = '#dc2626';
      }
    }
    </script>

    <!-- PDF preview panel (slides in from right) -->
    <div id="pdfPanel" style="display:none;position:fixed;top:0;right:0;width:45%;height:100vh;
         background:white;box-shadow:-4px 0 24px rgba(0,0,0,.15);z-index:1000;flex-direction:column">
      <div style="background:#0f2942;color:white;padding:12px 16px;display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:700;font-size:14px">📎 Invoice PDF</span>
        <button onclick="closePdf()"
          style="background:rgba(255,255,255,.15);color:white;border:none;border-radius:6px;
                 padding:4px 12px;cursor:pointer;font-weight:700">✕ Close</button>
      </div>
      <iframe id="pdfFrame" src="" style="flex:1;width:100%;height:calc(100vh - 48px);border:none"></iframe>
    </div>
    <script>
    function showPdf(url) {
      document.getElementById('pdfFrame').src = url;
      const panel = document.getElementById('pdfPanel');
      panel.style.display = 'flex';
      // Shrink main content to make room
      document.querySelector('.ml-52').style.marginRight = '45%';
    }
    function closePdf() {
      document.getElementById('pdfPanel').style.display = 'none';
      document.querySelector('.ml-52').style.marginRight = '0';
      document.getElementById('pdfFrame').src = '';
    }
    // Drop a wrongly-picked PDF before saving: clear the file, the preview, and any
    // fields the PDF auto-filled, so you can start clean or pick another.
    function removePdf() {
      const fi = document.getElementById('pdf_prefill');
      if (fi) fi.value = '';
      if (window._pdfObjUrl) { URL.revokeObjectURL(window._pdfObjUrl); window._pdfObjUrl = null; }
      closePdf();
      const rm = document.getElementById('pdf_remove'); if (rm) rm.style.display = 'none';
      const st = document.getElementById('pdf_status'); if (st) st.textContent = '';
      ['invoice_number','invoice_date','due_date','gross_amount','vat_amount','net_amount','payment_terms'].forEach(function(n){
        const el = document.querySelector('[name="'+n+'"]');
        if (el) { el.value=''; el.style.background=''; el.style.border=''; el.dataset.manual=''; el.dataset.needsInput=''; }
      });
    }
    // Edit mode: show the attached PDF side-by-side WITHOUT extracting/overwriting any
    // field — so you can read it while checking the record, and your entries stay untouched.
    function previewPdfOnly() {
      const fi = document.getElementById('pdf_prefill');
      if (!fi || !fi.files.length) return;
      try {
        if (window._pdfObjUrl) URL.revokeObjectURL(window._pdfObjUrl);
        window._pdfObjUrl = URL.createObjectURL(fi.files[0]);
        showPdf(window._pdfObjUrl);
      } catch(e) {}
      const st = document.getElementById('pdf_status');
      if (st) { st.textContent = '📎 PDF ready — saved when you press Update. Your entered details are unchanged.'; st.style.color = '#16a34a'; }
    }
    </script>"""

    # ── Pending approvals banner ──
    if pending_count > 0:
        flash += f"""<div style='background:#fef3c7;border:1px solid #fbbf24;border-radius:10px;
            padding:12px 16px;display:flex;justify-content:space-between;align-items:center'>
          <span style='font-weight:700;color:#92400e'>
            ⏳ {pending_count} invoice{'s' if pending_count>1 else ''} awaiting your approval
          </span>
          <a href='/invoices?ledger={ledger}&status=pending'
             style='background:#d97706;color:white;font-weight:700;padding:6px 14px;
                    border-radius:8px;font-size:13px;text-decoration:none'>
            Review Now →
          </a>
        </div>"""

    # ── Ledger switcher ──
    ledger_switcher = f"""
    <div class='flex flex-wrap gap-2 items-center'>
      <div class='text-xl font-black text-slate-800'>🧾 Invoice Manager</div>
      <select onchange="window.location='/invoices?ledger='+this.value"
        style='border:1px solid #e2e8f0;border-radius:8px;padding:6px 12px;font-size:14px;font-weight:600;max-width:260px'>
        {ledger_opts}
      </select>
    </div>
    <div class='flex flex-wrap gap-2 items-center justify-end' style='margin-top:8px'>
      <a href='/invoices/recent-payments' class='btn-secondary'>📋 Recent Payments</a>
      {"<a href='/invoices/dd-collection' class='btn-secondary'>🏦 DD Collection Check</a>" if user.get('role') == 'owner' else ''}
      {"<a href='/invoices/accountant-batch' class='btn-secondary'>📨 Send to Accountant</a>" if user.get('role') == 'owner' else ''}
      {"<a href='/invoices/reports' class='btn-secondary'>📊 Reports</a>" if user.get('role') == 'owner' else ''}
      {"<a href='/invoices/property-reports' class='btn-secondary'>🏠 Property Reports</a>" if user.get('role') == 'owner' else ''}
      {"<a href='/invoices/supplier-terms' class='btn-secondary'>📅 Supplier Terms</a>" if user.get('role') == 'owner' else ''}
    </div>"""

    # Auto-fill the due date from the supplier's payment-term rule.
    terms_js = f"""
    <script>
    const SUPPLIER_TERMS = {supplier_terms_js};
    (function() {{
      function fmt(d) {{ return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }}
      function applyTerms() {{
        const sup=document.querySelector('[name="supplier_name"]');
        const idate=document.querySelector('[name="invoice_date"]');
        const ddate=document.querySelector('[name="due_date"]');
        const terms=document.querySelector('[name="payment_terms"]');
        if(!sup||!idate||!ddate) return;
        const rule=SUPPLIER_TERMS[(sup.value||'').trim()];
        if(!rule||!idate.value) return;
        if(ddate.dataset.manual) return;              // owner overrode — leave it
        let due;
        if(rule.t==='days'){{ if(terms) terms.value=rule.v; const d=new Date(idate.value); d.setDate(d.getDate()+rule.v); due=d; }}
        else if(rule.t==='eom'){{ const d=new Date(idate.value); due=new Date(d.getFullYear(), d.getMonth()+rule.v+1, 0); }}
        if(due) ddate.value=fmt(due);
      }}
      document.addEventListener('DOMContentLoaded', function() {{
        const sup=document.querySelector('[name="supplier_name"]');
        const idate=document.querySelector('[name="invoice_date"]');
        if(sup){{ sup.addEventListener('change',applyTerms); sup.addEventListener('blur',applyTerms); }}
        if(idate) idate.addEventListener('change',applyTerms);
        // Auto-fill the due date from the supplier's term ONLY on a NEW invoice,
        // so opening a SAVED invoice never silently recomputes its stored due date.
        // (Deliberately changing the supplier/date on an edit still recalculates,
        // via the listeners above — that's a real change the owner is making.)
        var _ef = document.getElementById('invoiceForm');
        var _ea = (_ef && _ef.getAttribute('action')) || '';
        if (_ea.indexOf('/save/0') !== -1) applyTerms();
        // Owner/manager only: gently confirm before adding a brand-new supplier
        // name (staff use a fixed dropdown, so this is skipped for them).
        var dl = document.getElementById('supplierlist');
        if (sup && dl && sup.tagName !== 'SELECT') {{
          var f = sup.form;
          var known = new Set(Array.from(dl.options).map(function(o){{ return o.value.trim().toLowerCase(); }}));
          if (f) f.addEventListener('submit', function(e) {{
            var v = (sup.value||'').trim();
            if (v && !known.has(v.toLowerCase()) && !f.dataset.newsupok) {{
              e.preventDefault();
              if (confirm('"'+v+'" is not in your supplier list yet.\\n\\nThis may be a new supplier, or a typo/duplicate of one you already have.\\n\\nOK = add it as a NEW supplier.\\nCancel = go back and pick an existing one.')) {{
                f.dataset.newsupok = '1'; f.submit();
              }}
            }}
          }});
        }}
      }});
    }})();
    </script>"""

    # ── Query / activity notes log (edit mode) — a dated, per-invoice log for
    #    long-running supplier queries (emails, calls). Anyone logged in can add. ──
    notes_html = ""
    if is_edit:
        _src = "property" if is_prop else "supplier"
        _notes = q("""SELECT note, author, created_at FROM invoice_notes
                      WHERE source=? AND invoice_id=? ORDER BY created_at DESC, note_id DESC""",
                   (_src, edit_id), fetch=True) or []
        def _esc(s):
            return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        _rows = "".join(
            f"<div style='border-left:3px solid #cbd5e1;padding:3px 10px;margin-bottom:6px'>"
            f"<div style='font-size:11px;color:#94a3b8'>{_esc(n['author']) or '—'} · {fmt_uk_dt(n['created_at'])}</div>"
            f"<div style='font-size:13px;color:#334155;white-space:pre-wrap'>{_esc(n['note'])}</div></div>"
            for n in _notes)
        if not _rows:
            _rows = "<div style='font-size:12px;color:#94a3b8'>No notes yet — add the first one below.</div>"
        notes_html = f"""
        <div class='card' id='notes'>
          <div class='text-xs font-bold text-slate-500 uppercase tracking-wide mb-2'>📝 Query / activity notes</div>
          <div style='font-size:11px;color:#94a3b8;margin-bottom:8px'>A dated log for chasing supplier queries.
            Each note is stamped with who added it and when. Notes are kept — they are not overwritten.</div>
          <form method='POST' action='/invoices/add-note' style='display:flex;gap:8px;margin-bottom:12px'>
            <input type='hidden' name='ledger' value='{ledger}'>
            <input type='hidden' name='invoice_id' value='{edit_id}'>
            <input type='text' name='note' required maxlength='500'
              placeholder='Add a note (e.g. Emailed supplier 02/07 about 2 missing items, awaiting reply)'
              style='flex:1;padding:6px 10px'>
            <button type='submit' class='btn-secondary'>➕ Add note</button>
          </form>
          {_rows}
        </div>"""

    # ── Additional documents (edit mode): extra whole files per invoice (demand
    #    note, supporting emails…). Anyone logged in can add; only owner/manager
    #    can remove. The primary invoice scan stays in the form above (pdf_path). ──
    attachments_html = ""
    if is_edit:
        _asrc = "property" if is_prop else "supplier"
        _atts = q("""SELECT att_id, orig_name, label, uploaded_by FROM invoice_attachments
                     WHERE source=? AND invoice_id=? ORDER BY uploaded_at, att_id""",
                  (_asrc, edit_id), fetch=True) or []
        _can_del = user.get("role") in ("owner", "manager")
        _led = html.escape(str(ledger), quote=True)
        _view_exts = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        _alist = ""
        for a in _atts:
            _name = html.escape(str(a['orig_name'] or 'document'))
            _ext  = os.path.splitext(str(a['orig_name'] or ''))[1].lower()
            _href = f"/invoices/attachment/{a['att_id']}"
            if _ext in _view_exts:   # PDF / image → open in the side-by-side panel
                _link = (f"<a href='#' onclick=\"showPdf('{_href}');return false;\" "
                         f"style='color:#0369a1;font-weight:600;text-decoration:none' title='View side-by-side'>📄 {_name}</a>")
            else:                    # Word / .msg etc — can't preview inline, open normally
                _link = (f"<a href='{_href}' target='_blank' "
                         f"style='color:#0369a1;font-weight:600;text-decoration:none' "
                         f"title='Opens in a new tab (this file type cannot preview side-by-side)'>📄 {_name}</a>")
            _lbl = f" <span style='color:#0f766e;font-size:12px;font-weight:700'>— {html.escape(str(a['label']))}</span>" if a['label'] else ""
            _del = ""
            if _can_del:
                _del = (f"<form method='POST' action='/invoices/attachment/{a['att_id']}/delete' style='display:inline'"
                        f" onsubmit=\"return confirm('Remove this document?');\">"
                        f"<input type='hidden' name='ledger' value='{_led}'>"
                        f"<input type='hidden' name='invoice_id' value='{edit_id}'>"
                        f"<button type='submit' class='btn-secondary' style='font-size:11px;color:#dc2626'>🗑 Remove</button></form>")
            _alist += (f"<div style='display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #cbe8e4'>"
                       f"{_link}{_lbl}"
                       f"<span style='font-size:11px;color:#94a3b8;margin-left:auto'>{html.escape(str(a['uploaded_by'] or ''))}</span>{_del}</div>")
        if not _atts:
            _alist = "<div style='font-size:13px;color:#64748b;padding:4px 0'>No supporting documents attached yet.</div>"
        import json
        _existing_js = json.dumps([str(a['orig_name'] or '').strip().lower() for a in _atts])
        attachments_html = f"""
        <div class='card' id='attachments' style='border-left:5px solid #0d9488;background:#f0fdfa;margin-top:12px'>
          <div style='font-weight:900;color:#0f766e;margin-bottom:4px'>📎 Supporting documents for this invoice</div>
          <div style='font-size:11px;color:#475569;margin-bottom:10px'>Demand notes, supplier emails, or any extra files for this invoice — separate from the main invoice PDF above. Pick a file and it previews <b>side-by-side</b> so you can check it's the right one; click an attached PDF/image to view it side-by-side too. Give each a short <b>label</b>. Only the owner can remove.</div>
          {_alist}
          <form method='POST' action='/invoices/attachment/add' enctype='multipart/form-data' onsubmit='return checkDupAttach(this)' style='margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap'>
            <input type='hidden' name='ledger' value='{_led}'>
            <input type='hidden' name='invoice_id' value='{edit_id}'>
            <input type='file' name='attach_files' multiple required onchange='previewAttachOnPick(this)' style='font-size:13px'>
            <input type='text' name='label' required placeholder='label (required, e.g. Demand note)' maxlength='60' style='font-size:13px;padding:6px 10px;border:1px solid #cbd5e1;border-radius:8px'>
            <button type='submit' class='btn-secondary' style='font-size:13px'>➕ Attach file(s)</button>
          </form>
          <script>
          function previewAttachOnPick(inp){{
            if(!inp.files || !inp.files.length) return;
            var f = inp.files[0];
            var ext = (f.name.split('.').pop()||'').toLowerCase();
            if(['pdf','png','jpg','jpeg','gif','webp','bmp'].indexOf(ext) === -1) return;  // can't preview others
            try {{
              if(window._attObjUrl) URL.revokeObjectURL(window._attObjUrl);
              window._attObjUrl = URL.createObjectURL(f);
              showPdf(window._attObjUrl);
            }} catch(e){{}}
          }}
          var _existingAtt = {_existing_js};
          function checkDupAttach(form){{
            var inp = form.querySelector('[name="attach_files"]');
            if(!inp || !inp.files || !inp.files.length) return true;
            for(var i=0;i<inp.files.length;i++){{
              var nm = (inp.files[i].name||'').trim().toLowerCase();
              if(_existingAtt.indexOf(nm) !== -1){{
                if(!confirm('A file named "'+inp.files[i].name+'" is already attached to this invoice.\\n\\nAttach it again anyway?')) return false;
              }}
            }}
            return true;
          }}
          </script>
        </div>"""

    vat_js = f"""
    <script>
    (function(){{
      var SUPPLIER_VAT = {supplier_vat_js};
      var vat   = document.querySelector('[name="vat_amount"]');
      var gross = document.querySelector('[name="gross_amount"]');
      var sup   = document.querySelector('[name="supplier_name"]');
      var claim = document.querySelector('[name="claimable_vat"]');
      var note  = document.getElementById('vatnote');
      function pct(){{
        var name = sup ? (sup.value||'').trim() : '';
        return (SUPPLIER_VAT[name] != null) ? SUPPLIER_VAT[name] : 100;
      }}
      function showNote(){{
        if(!note) return;
        var p = pct();
        note.textContent = (p < 100) ? ('Only ' + p + '% of the VAT is reclaimable for this supplier (e.g. company car — HMRC).') : '';
      }}
      function recalcClaimable(){{
        showNote();
        if(!vat || !claim) return;
        if(claim.dataset.manual) return;      // owner typed a value — leave it
        var v = parseFloat(vat.value) || 0;
        claim.value = (v * pct() / 100).toFixed(2);
      }}
      if(claim) claim.addEventListener('input', function(){{ claim.dataset.manual='1'; }});
      if(vat)   vat.addEventListener('input', recalcClaimable);
      if(gross) gross.addEventListener('input', function(){{ setTimeout(recalcClaimable, 0); }});
      if(sup){{ sup.addEventListener('change', recalcClaimable); sup.addEventListener('blur', recalcClaimable); }}
      // On load: always show the note; only AUTO-fill the figure for a NEW invoice,
      // so opening a saved invoice never overwrites its stored claimable VAT.
      showNote();
      var f = document.getElementById('invoiceForm');
      if(f && (f.getAttribute('action')||'').indexOf('/save/0') !== -1) recalcClaimable();
    }})();
    </script>
    """
    content = "\n".join([flash, ledger_switcher, summary, search_bar, attachments_html, form_html, notes_html, list_html, js, terms_js, vat_js])
    return page("Invoices", content, user, "invoices")


@router.post("/invoices/add-note")
async def add_note(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    form = await request.form()
    ledger = form.get("ledger", "Uxbridge")
    try:
        iid = int(form.get("invoice_id") or 0)
    except (TypeError, ValueError):
        iid = 0
    note = (form.get("note") or "").strip()
    if iid and note:
        src = "property" if is_property_ledger(ledger) else "supplier"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        q("""INSERT INTO invoice_notes (source, invoice_id, note, author, created_at)
             VALUES (?,?,?,?,?)""", (src, iid, note[:500], user.get("username", ""), now))
    from urllib.parse import quote as urlquote
    return RedirectResponse(f"/invoices?ledger={urlquote(ledger)}&edit_id={iid}#notes",
                            status_code=303)


@router.post("/invoices/save/{invoice_id}")
async def save_invoice(
    request:    Request,
    invoice_id: int,
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir

    form   = await request.form()
    ledger = form.get("ledger", "Uxbridge")
    is_prop = is_property_ledger(ledger)
    table   = "property_invoices" if is_prop else "supplier_invoices"
    loc_col = "property_name"     if is_prop else "store_name"
    loc_val = prop_name(ledger)   if is_prop else ledger

    def fv(key, default=""):
        v = form.get(key, default)
        return v.strip() if isinstance(v, str) else v

    def fnum(key):
        # Round money to whole pence so we never store repeating decimals
        # (e.g. VAT = gross / 6), which keeps the amount fields clean.
        try: return round(float(form.get(key, 0) or 0), 2)
        except: return 0.0

    def fint(key):
        try: return int(form.get(key, 0) or 0)
        except: return None

    # Handle PDF upload
    pdf_path = None
    pdf_file = form.get("pdf_file")
    if pdf_file and hasattr(pdf_file, "filename") and pdf_file.filename:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        ext      = os.path.splitext(pdf_file.filename)[1].lower()
        filename = f"{uuid.uuid4().hex}{ext}"
        full_path = os.path.join(UPLOAD_DIR, filename)
        with open(full_path, "wb") as f:
            f.write(await pdf_file.read())
        pdf_path = full_path

    supplier   = fv("supplier_name")
    inv_no     = (fv("invoice_number") or "").upper()
    inv_date   = fv("invoice_date") or None
    due_date   = fv("due_date")     or None
    gross      = fnum("gross_amount")
    vat        = fnum("vat_amount")
    net        = fnum("net_amount")
    claimable  = fnum("claimable_vat")
    # If claimable VAT wasn't supplied (e.g. a new invoice with JS off), derive it
    # from the supplier's VAT reclaim % (100% for all except ones you've set, e.g. Alphabet 50%).
    if not claimable and vat:
        _rp = q("SELECT vat_reclaim_pct FROM supplier_terms WHERE supplier_name=?", (supplier,), fetch=True)
        _pct = (_rp[0]["vat_reclaim_pct"] if _rp and _rp[0]["vat_reclaim_pct"] is not None else 100)
        claimable = round((vat or 0) * _pct / 100.0, 2)
    terms      = fint("payment_terms")
    comments   = fv("comments")     or None
    is_paid    = fv("is_paid", "No")
    paid_date  = fv("paid_date")    or None
    pay_method = fv("payment_method") or None
    amt_paid   = fnum("amount_paid")
    credit     = fnum("credit_note")
    seq_no     = fint("seq_no")
    exp_type   = fv("expense_type") or None
    dd_stmt    = fv("dd_statement_date") or None
    chq_no     = fv("cheque_number") or None
    acct_sent  = fv("accountant_sent_date") or None
    demand_ref = fv("demand_ref") or None
    linked_ref = fv("linked_ref") or None
    under_query = "Yes" if fv("under_query") else None
    # "Awaiting VAT invoice" is derived automatically: a demand/pro-forma ref is
    # present with no invoice number yet. Owner/manager can force it via the
    # override tick for the rare case that has no demand ref.
    awaiting   = "Yes" if (demand_ref and not inv_no) else None
    if user.get("role") in ("owner", "manager") and fv("awaiting_override"):
        awaiting = "Yes"

    # Staff cannot change payment details — on a staff edit, keep whatever is
    # already on the record (their form's payment fields are display-only/locked).
    if invoice_id != 0 and user.get("role") == "staff":
        _cols = "is_paid, paid_date, payment_method, amount_paid, credit_note, cheque_number"
        if not is_prop:
            _cols += ", dd_statement_date"
        _ex = q(f"SELECT {_cols} FROM {table} WHERE invoice_id=?", (invoice_id,), fetch=True)
        if _ex:
            e = dict(_ex[0])
            is_paid    = e.get("is_paid") or "No"
            paid_date  = e.get("paid_date")
            pay_method = e.get("payment_method")
            amt_paid   = e.get("amount_paid") or 0
            credit     = e.get("credit_note") or 0
            chq_no     = e.get("cheque_number")
            if not is_prop:
                dd_stmt = e.get("dd_statement_date")

    # Auto-set Direct Debit on a NEW invoice for suppliers that pay by DD (their
    # pays_dd flag on the supplier-terms screen). New invoices have no payment
    # section, so it's applied here; it only fills a blank method, never overrides one.
    if invoice_id == 0 and not pay_method and supplier:
        _dd = q("SELECT 1 FROM supplier_terms WHERE supplier_name=? AND pays_dd='Yes'",
                (supplier,), fetch=True)
        if _dd:
            pay_method = "Direct Debit"

    if not supplier:
        return RedirectResponse(f"/invoices?ledger={ledger}&msg=Supplier+name+is+required&msg_type=error",
                                status_code=303)

    from urllib.parse import quote as urlquote

    # ── Serial number must stay unique within its own sequence (retail or
    #    property — checked against the current ledger's table) ──
    if seq_no:
        clash = q(f"SELECT invoice_id FROM {table} WHERE seq_no=? AND invoice_id<>?",
                  (seq_no, invoice_id), fetch=True)
        if clash:
            return RedirectResponse(
                f"/invoices?ledger={ledger}&msg="
                + urlquote(f"Serial No. {seq_no} is already used. Please use a different number.")
                + "&msg_type=error", status_code=303)

    # ── Approval status: owner/manager entries are approved by default, but they
    #    may tick "Mark as pending" to park an entry for later review. ──
    role = user.get("role", "staff")
    submitted_by    = user.get("username", "")
    if role in ("owner", "manager"):
        approval_status = "pending" if fv("save_pending") else "approved"
    else:
        approval_status = "pending"

    # ── Staff can only file against an EXISTING supplier (they pick from a fixed
    #    dropdown). Enforce it server-side too, so the tidy supplier list can't be
    #    bypassed. Owner/manager instead get a soft client-side confirm. ──
    if role == "staff":
        _known = q("""SELECT 1 FROM (SELECT supplier_name FROM supplier_invoices
                        UNION SELECT supplier_name FROM property_invoices)
                      WHERE supplier_name=? LIMIT 1""", (supplier,), fetch=True)
        if not _known:
            return RedirectResponse(
                f"/invoices?ledger={ledger}&msg="
                + urlquote("Please pick a supplier from the list — new suppliers are added by the owner.")
                + "&msg_type=error", status_code=303)

    # ── Duplicate check (supplier + invoice_number + store, warn only) ──
    force = fv("force_save")
    if invoice_id == 0 and inv_no and not force:
        dup = q(f"SELECT invoice_id, supplier_name FROM {table} WHERE {loc_col}=? AND supplier_name=? AND invoice_number=?",
                (loc_val, supplier, inv_no), fetch=True)
        if dup:
            # Return duplicate warning page
            warn_url = f"/invoices?ledger={ledger}&edit_id={dup[0]['invoice_id']}"
            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;700;900&display=swap" rel="stylesheet">
<style>body{{font-family:'DM Sans',sans-serif;background:#f8fafc;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}</style>
</head><body>
<div style='background:white;border-radius:20px;padding:40px;max-width:480px;width:90%;border:2px solid #fbbf24;box-shadow:0 8px 32px rgba(0,0,0,.08)'>
  <div style='font-size:40px;text-align:center;margin-bottom:16px'>⚠️</div>
  <h2 style='font-weight:900;color:#92400e;text-align:center;margin:0 0 8px'>Possible Duplicate Invoice</h2>
  <p style='color:#64748b;font-size:14px;text-align:center;margin:0 0 20px'>
    An invoice from <strong>{supplier}</strong> with number <strong>{inv_no}</strong>
    already exists in {loc_val}.
  </p>
  <div style='background:#fef3c7;border-radius:10px;padding:12px 16px;font-size:13px;color:#92400e;margin-bottom:24px'>
    This may be a genuine duplicate. Check the existing record before saving again.
  </div>
  <div style='display:flex;flex-direction:column;gap:10px'>
    <a href='{warn_url}' style='background:#1e3a5f;color:white;font-weight:700;padding:12px;border-radius:10px;text-align:center;text-decoration:none;font-size:14px'>
      👁️ View Existing Invoice
    </a>
    <form method='POST' action='/invoices/save/0'>
      <input type='hidden' name='ledger'          value='{ledger}'>
      <input type='hidden' name='supplier_name'   value='{html.escape(str(supplier), quote=True)}'>
      <input type='hidden' name='invoice_number'  value='{html.escape(str(inv_no), quote=True)}'>
      <input type='hidden' name='invoice_date'    value='{fv("invoice_date")}'>
      <input type='hidden' name='due_date'        value='{fv("due_date")}'>
      <input type='hidden' name='gross_amount'    value='{gross}'>
      <input type='hidden' name='vat_amount'      value='{vat}'>
      <input type='hidden' name='net_amount'      value='{net}'>
      <input type='hidden' name='payment_terms'   value='{terms or ""}'>
      <input type='hidden' name='comments'        value='{html.escape(fv("comments") or "", quote=True)}'>
      <input type='hidden' name='seq_no'          value='{seq_no or ""}'>
      <input type='hidden' name='force_save'      value='1'>
      <button type='submit' style='width:100%;background:#dc2626;color:white;font-weight:700;padding:12px;border-radius:10px;font-size:14px;border:none;cursor:pointer'>
        ⚠️ Save Anyway (Different Supplier?)
      </button>
    </form>
    <a href='/invoices?ledger={ledger}' style='color:#64748b;text-align:center;font-size:13px;text-decoration:none'>← Cancel, go back</a>
  </div>
</div>
</body></html>""")

    # ── Validation warnings (non-blocking) — shown ONCE on-screen after saving,
    #    NOT written into the comments (that used to pile up on every edit).
    #    "Due date is in the past" dropped: the Overdue flag already covers it.
    warnings = []
    if gross > 0 and vat > 0:
        expected_vat = round(gross / 6, 2)
        if abs(vat - expected_vat) > 1.0:
            warnings.append(f"VAT £{vat:.2f} doesn't match standard 20% (expected ~£{expected_vat:.2f})")
    if gross > 10000:
        warnings.append(f"Large invoice amount: £{gross:,.2f} — please double-check")

    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # On edit, only owner/manager may change the approved/pending state (via the
    # "Mark as pending" tick); a staff edit leaves the status untouched.
    appr_set = ", approval_status=?" if role in ("owner", "manager") else ""
    appr_val = [approval_status] if role in ("owner", "manager") else []
    # Linked Invoice / CN Ref is owner-only, so only an owner edit touches it.
    lnk_set = ", linked_ref=?" if role == "owner" else ""
    lnk_val = [linked_ref] if role == "owner" else []

    if invoice_id == 0:
        # New invoice
        if is_prop:
            q(f"""INSERT INTO {table}
                (seq_no, property_name, supplier_name, invoice_number, invoice_date,
                 expense_type, gross_amount, vat_amount, net_amount, due_date,
                 paid_date, amount_paid, is_paid, payment_method, cheque_number,
                 accountant_sent_date, comments, pdf_path, approval_status,
                 submitted_by, created_at, awaiting_invoice, demand_ref, linked_ref, under_query, claimable_vat)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (seq_no, loc_val, supplier, inv_no, inv_date, exp_type,
               gross, vat, net, due_date, paid_date, amt_paid, is_paid, pay_method,
               chq_no, acct_sent, comments, pdf_path, approval_status,
               submitted_by, now_ts, awaiting, demand_ref, linked_ref, under_query, claimable))
        else:
            q(f"""INSERT OR IGNORE INTO {table}
                (store_name, seq_no, supplier_name, invoice_number, invoice_date,
                 gross_amount, vat_amount, net_amount, due_date, payment_terms,
                 comments, is_paid, payment_method, pdf_path, approval_status, submitted_by, created_at,
                 awaiting_invoice, demand_ref, linked_ref, under_query, claimable_vat)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (loc_val, seq_no, supplier, inv_no, inv_date,
               gross, vat, net, due_date, terms, comments, is_paid, pay_method, pdf_path,
               approval_status, submitted_by, now_ts, awaiting, demand_ref, linked_ref, under_query, claimable))
        if approval_status == "pending":
            msg = f"Invoice submitted for approval — {supplier} {inv_no}"
        else:
            msg = f"Invoice added — {supplier} {inv_no}"
    else:
        # Update existing
        if is_prop:
            acct_set = ", accountant_sent_date=?" if role == "owner" else ""
            acct_val = [acct_sent] if role == "owner" else []
            q(f"""UPDATE {table} SET
                seq_no=?, supplier_name=?, invoice_number=?, invoice_date=?,
                expense_type=?, gross_amount=?, vat_amount=?, net_amount=?,
                due_date=?, comments=?, is_paid=?,
                paid_date=?, payment_method=?, amount_paid=?, credit_note=?,
                cheque_number=?, awaiting_invoice=?, demand_ref=?, under_query=?, claimable_vat=?{acct_set}{appr_set}{lnk_set},
                updated_by=?, updated_at=?
                {', pdf_path=?' if pdf_path else ''}
                WHERE invoice_id=?""",
              ([seq_no, supplier, inv_no, inv_date, exp_type, gross, vat, net,
                due_date, comments, is_paid, paid_date, pay_method, amt_paid, credit,
                chq_no, awaiting, demand_ref, under_query, claimable] + acct_val + appr_val + lnk_val + [submitted_by, now_ts]
               + ([pdf_path] if pdf_path else []) + [invoice_id]))
        else:
            # Only the owner sees/edits "Sent to Accountant", so only touch it on
            # an owner edit — otherwise a staff edit (no such field) would blank it.
            acct_set = ", accountant_sent_date=?" if role == "owner" else ""
            acct_val = [acct_sent] if role == "owner" else []
            q(f"""UPDATE {table} SET
                seq_no=?, supplier_name=?, invoice_number=?, invoice_date=?,
                gross_amount=?, vat_amount=?, net_amount=?,
                due_date=?, payment_terms=?, comments=?, is_paid=?,
                paid_date=?, payment_method=?, amount_paid=?, credit_note=?,
                dd_statement_date=?, cheque_number=?,
                awaiting_invoice=?, demand_ref=?, under_query=?, claimable_vat=?{acct_set}{appr_set}{lnk_set},
                updated_by=?, updated_at=?
                {', pdf_path=?' if pdf_path else ''}
                WHERE invoice_id=?""",
              ([seq_no, supplier, inv_no, inv_date, gross, vat, net,
                due_date, terms, comments, is_paid, paid_date,
                pay_method, amt_paid, credit, dd_stmt, chq_no, awaiting, demand_ref, under_query, claimable]
               + acct_val + appr_val + lnk_val + [submitted_by, now_ts]
               + ([pdf_path] if pdf_path else []) + [invoice_id]))
        msg = f"Invoice updated — {supplier} {inv_no}"

    from urllib.parse import quote as urlquote
    if warnings:
        msg = msg + "  ⚠️ Please check: " + "; ".join(warnings)
    return RedirectResponse(
        f"/invoices?ledger={ledger}&msg={urlquote(msg)}&msg_type=success#invoice-form",
        status_code=303)


@router.get("/invoices/delete/{invoice_id}")
def delete_invoice(
    invoice_id: int,
    ledger:     str = "Uxbridge",
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] != "owner":
        return RedirectResponse(f"/invoices?ledger={ledger}&msg=Only+the+owner+can+delete+invoices&msg_type=error",
                                status_code=303)
    table = "property_invoices" if is_property_ledger(ledger) else "supplier_invoices"
    # Grab the attached PDF path before deleting the row, so we can also remove
    # the file — otherwise deleting an invoice leaves an orphaned PDF behind.
    old = q(f"SELECT pdf_path FROM {table} WHERE invoice_id=?", (invoice_id,), fetch=True)
    q(f"DELETE FROM {table} WHERE invoice_id=?", (invoice_id,))
    if old and old[0]["pdf_path"]:
        try:
            if os.path.exists(old[0]["pdf_path"]):
                os.remove(old[0]["pdf_path"])
        except OSError:
            pass  # file already gone / locked — not worth failing the delete over
    # Also remove any additional attachments (files + rows) for this invoice.
    _src = "property" if is_property_ledger(ledger) else "supplier"
    for a in (q("SELECT file_path FROM invoice_attachments WHERE source=? AND invoice_id=?",
                (_src, invoice_id), fetch=True) or []):
        if a["file_path"]:
            try:
                if os.path.exists(a["file_path"]):
                    os.remove(a["file_path"])
            except OSError:
                pass
    q("DELETE FROM invoice_attachments WHERE source=? AND invoice_id=?", (_src, invoice_id))
    from urllib.parse import quote as urlquote
    return RedirectResponse(
        f"/invoices?ledger={ledger}&msg={urlquote('Invoice deleted')}&msg_type=success",
        status_code=303)


@router.get("/invoices/pdf/{invoice_id}")
def serve_pdf(
    invoice_id: int,
    ledger:     str = "Uxbridge",
    session:    str | None = Cookie(default=None)
):
    from fastapi.responses import FileResponse
    redir, user = require_login(session)
    if redir: return redir
    table = "property_invoices" if is_property_ledger(ledger) else "supplier_invoices"
    rows  = q(f"SELECT pdf_path FROM {table} WHERE invoice_id=?", (invoice_id,), fetch=True)
    if rows and rows[0]["pdf_path"] and os.path.exists(rows[0]["pdf_path"]):
        return FileResponse(rows[0]["pdf_path"], media_type="application/pdf")
    return HTMLResponse("<p>PDF not found</p>", status_code=404)


@router.post("/invoices/pdf/{invoice_id}/remove-page")
def remove_pdf_page(invoice_id: int, ledger: str = Form("Uxbridge"),
                    page: int = Form(0), session: str | None = Cookie(default=None)):
    """Remove one page from an invoice's attached PDF (e.g. a store report scanned
    in with the invoice). Owner/manager only; keeps a one-time .bak of the original."""
    import shutil
    from urllib.parse import quote as urlquote
    redir, user = require_login(session)
    if redir: return redir
    table = "property_invoices" if is_property_ledger(ledger) else "supplier_invoices"

    def _back(msg, err=False):
        tail = "&msg_type=error" if err else ""
        return RedirectResponse(f"/invoices?ledger={ledger}&edit_id={invoice_id}&msg={urlquote(msg)}{tail}",
                                status_code=303)

    if user.get("role") not in ("owner", "manager"):
        return _back("Only the owner or a manager can edit the PDF", err=True)
    rows = q(f"SELECT pdf_path FROM {table} WHERE invoice_id=?", (invoice_id,), fetch=True)
    path = rows[0]["pdf_path"] if rows and rows[0]["pdf_path"] else None
    if not path or not os.path.exists(path) or not path.lower().endswith(".pdf"):
        return _back("No PDF to edit here", err=True)
    try:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(path)
        n = len(reader.pages)
        if n <= 1:
            return _back("This PDF has only one page — use 'delete attachment' instead", err=True)
        if page < 1 or page > n:
            return _back("That page number isn't in the PDF", err=True)
        if not os.path.exists(path + ".bak"):          # one-time backup of the original
            shutil.copy(path, path + ".bak")
        writer = PdfWriter()
        for i, pg in enumerate(reader.pages, start=1):
            if i != page:
                writer.add_page(pg)
        with open(path, "wb") as f:
            writer.write(f)
        return _back(f"Removed page {page} — the PDF now has {n - 1} page(s).")
    except Exception:
        return _back("Sorry, couldn't edit that PDF", err=True)


@router.get("/invoices/pdf-thumb/{invoice_id}")
def serve_pdf_thumb(
    invoice_id: int,
    ledger:     str = "Uxbridge",
    session:    str | None = Cookie(default=None)
):
    """Render the first page of an attached PDF (or return an image attachment)
    as a small PNG thumbnail for previewing in the invoice form."""
    redir, user = require_login(session)
    if redir: return redir
    table = "property_invoices" if is_property_ledger(ledger) else "supplier_invoices"
    rows  = q(f"SELECT pdf_path FROM {table} WHERE invoice_id=?", (invoice_id,), fetch=True)
    path  = rows[0]["pdf_path"] if rows and rows[0]["pdf_path"] else None
    if not path or not os.path.exists(path):
        return Response(status_code=404)

    ext = os.path.splitext(path)[1].lower()
    try:
        from PIL import Image
        thumb = io.BytesIO()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            img = Image.open(path)
        else:  # treat as PDF — render page 1
            import pypdfium2 as pdfium
            pdf  = pdfium.PdfDocument(path)
            img  = pdf[0].render(scale=2.0).to_pil()
        img.thumbnail((600, 850))
        img.convert("RGB").save(thumb, "PNG")
        return Response(thumb.getvalue(), media_type="image/png")
    except Exception:
        return Response(status_code=404)


@router.post("/invoices/attachment/add")
async def attachment_add(request: Request, session: str | None = Cookie(default=None)):
    """Attach one or more ADDITIONAL documents to a saved invoice (demand note,
    supporting emails, etc.). Any logged-in user may add; only owner/manager can
    remove (see attachment_delete). The main invoice PDF is unchanged (pdf_path)."""
    redir, user = require_login(session)
    if redir: return redir
    from urllib.parse import quote as urlquote
    form   = await request.form()
    ledger = str(form.get("ledger", "Uxbridge"))
    try:    invoice_id = int(form.get("invoice_id") or 0)
    except (TypeError, ValueError): invoice_id = 0
    label  = (str(form.get("label", "")) or "").strip()
    source = "property" if is_property_ledger(ledger) else "supplier"
    if not invoice_id:
        return RedirectResponse(f"/invoices?ledger={ledger}&msg=Save+the+invoice+first,+then+add+documents&msg_type=error",
                                status_code=303)
    files = [f for f in form.getlist("attach_files") if hasattr(f, "filename") and f.filename]
    if not files:
        return RedirectResponse(f"/invoices?ledger={ledger}&edit_id={invoice_id}&msg=No+file+chosen&msg_type=error#attachments",
                                status_code=303)
    if not label:
        return RedirectResponse(f"/invoices?ledger={ledger}&edit_id={invoice_id}&msg={urlquote('Please add a short label describing the document(s).')}&msg_type=error#attachments",
                                status_code=303)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    saved = 0
    for f in files:
        ext  = os.path.splitext(f.filename)[1].lower()
        fn   = f"{uuid.uuid4().hex}{ext}"
        full = os.path.join(UPLOAD_DIR, fn)
        with open(full, "wb") as out:
            out.write(await f.read())
        q("""INSERT INTO invoice_attachments (source, invoice_id, file_path, orig_name, label, uploaded_by)
             VALUES (?,?,?,?,?,?)""",
          (source, invoice_id, full, f.filename, label or None, user.get("username", "")))
        saved += 1
    return RedirectResponse(f"/invoices?ledger={ledger}&edit_id={invoice_id}&msg={urlquote(f'{saved} document(s) attached')}#attachments",
                            status_code=303)


@router.get("/invoices/attachment/{att_id}")
def attachment_serve(att_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    rows = q("SELECT file_path FROM invoice_attachments WHERE att_id=?", (att_id,), fetch=True)
    if not rows or not rows[0]["file_path"] or not os.path.exists(rows[0]["file_path"]):
        return HTMLResponse("<p>Attachment not found</p>", status_code=404)
    path = rows[0]["file_path"]
    ext  = os.path.splitext(path)[1].lower()
    mt   = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=mt)


@router.post("/invoices/attachment/{att_id}/delete")
def attachment_delete(att_id: int, ledger: str = Form("Uxbridge"),
                      invoice_id: int = Form(0), session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    from urllib.parse import quote as urlquote
    if user.get("role") not in ("owner", "manager"):
        return RedirectResponse(
            f"/invoices?ledger={ledger}&edit_id={invoice_id}&msg=Only+the+owner+can+remove+documents&msg_type=error#attachments",
            status_code=303)
    rows = q("SELECT file_path FROM invoice_attachments WHERE att_id=?", (att_id,), fetch=True)
    q("DELETE FROM invoice_attachments WHERE att_id=?", (att_id,))
    if rows and rows[0]["file_path"]:
        try:
            if os.path.exists(rows[0]["file_path"]):
                os.remove(rows[0]["file_path"])
        except OSError:
            pass
    return RedirectResponse(
        f"/invoices?ledger={ledger}&edit_id={invoice_id}&msg={urlquote('Document removed')}#attachments",
        status_code=303)


@router.post("/invoices/extract-pdf")
async def extract_pdf_ajax(request: Request, session: str | None = Cookie(default=None)):
    """Receive a PDF upload, extract fields, return JSON for JS to fill the form."""
    redir, user = require_login(session)
    if redir: return JSONResponse({"error": "Not logged in"}, status_code=401)
    form = await request.form()
    pdf_file = form.get("pdf_file")
    if not pdf_file or not hasattr(pdf_file, "read"):
        return JSONResponse({"error": "No file"})
    data = extract_pdf_data(await pdf_file.read())
    return JSONResponse(data)


@router.get("/invoices/approve/{invoice_id}")
def approve_invoice(
    invoice_id: int,
    ledger:     str = "Uxbridge",
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner", "manager"):
        return RedirectResponse(f"/invoices?ledger={ledger}&msg=Not+authorised&msg_type=error",
                                status_code=303)
    table = "property_invoices" if is_property_ledger(ledger) else "supplier_invoices"
    q(f"UPDATE {table} SET approval_status='approved' WHERE invoice_id=?", (invoice_id,))
    from urllib.parse import quote as urlquote
    return RedirectResponse(
        f"/invoices?ledger={ledger}&msg={urlquote('Invoice approved ✅')}&msg_type=success",
        status_code=303)


@router.get("/invoices/reject/{invoice_id}")
def reject_invoice(
    invoice_id: int,
    ledger:     str = "Uxbridge",
    session:    str | None = Cookie(default=None)
):
    redir, user = require_login(session)
    if redir: return redir
    if user["role"] not in ("owner", "manager"):
        return RedirectResponse(f"/invoices?ledger={ledger}&msg=Not+authorised&msg_type=error",
                                status_code=303)
    table = "property_invoices" if is_property_ledger(ledger) else "supplier_invoices"
    q(f"UPDATE {table} SET approval_status='rejected' WHERE invoice_id=?", (invoice_id,))
    from urllib.parse import quote as urlquote
    return RedirectResponse(
        f"/invoices?ledger={ledger}&msg={urlquote('Invoice rejected and flagged')}&msg_type=error",
        status_code=303)


@router.get("/invoices/recent-payments", response_class=HTMLResponse)
def recent_payments(session: str | None = Cookie(default=None), scope: str = ""):
    redir, user = require_login(session)
    if redir: return redir

    from collections import defaultdict

    # Scope lets you focus on one ledger — useful because property payments can
    # be much older than the busy store ones and would otherwise drop off the
    # most-recent list.
    want_retail = scope in ("", "Uxbridge", "Newbury")
    want_prop   = scope in ("", "Property")
    parts, params = [], []
    if want_retail:
        rc = "(paid_date IS NOT NULL OR amount_paid > 0)"
        if scope in ("Uxbridge", "Newbury"):
            rc += " AND store_name=?"; params.append(scope)
        parts.append(f"""
            SELECT 'retail' as ledger_type, store_name as location,
                   supplier_name, invoice_number, gross_amount,
                   amount_paid, credit_note, paid_date, payment_method, is_paid,
                   COALESCE(gross_amount,0)-COALESCE(amount_paid,0)-COALESCE(credit_note,0) as balance
            FROM supplier_invoices WHERE {rc}""")
    if want_prop:
        parts.append("""
            SELECT 'property' as ledger_type, property_name as location,
                   supplier_name, invoice_number, gross_amount,
                   amount_paid, 0 as credit_note, paid_date, payment_method, is_paid,
                   COALESCE(gross_amount,0)-COALESCE(amount_paid,0) as balance
            FROM property_invoices WHERE (paid_date IS NOT NULL OR amount_paid > 0)""")
    rows = q(" UNION ALL ".join(parts) + " ORDER BY paid_date DESC LIMIT 300",
             params, fetch=True) or []

    scope_opts = "".join(
        f"<option value='{v}' {'selected' if scope==v else ''}>{lbl}</option>"
        for v, lbl in [("", "Everything"), ("Uxbridge", "Uxbridge"),
                       ("Newbury", "Newbury"), ("Property", "Properties")])

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["paid_date"] or "Unknown"].append(r)

    rows_html = ""
    for date_key in sorted(by_date.keys(), reverse=True):
        day_rows  = by_date[date_key]
        day_total = sum(r["amount_paid"] or 0 for r in day_rows)
        rows_html += f"""
        <tr style='background:#f8fafc'>
          <td colspan='8' style='font-weight:900;color:#0f2942;padding:10px 12px;font-size:13px'>
            📅 {fmt_uk_date(date_key)}
            <span style='float:right;color:#16a34a;font-weight:700'>Day total: £{day_total:,.2f}</span>
          </td>
        </tr>"""
        for r in day_rows:
            paid    = r["amount_paid"] or 0
            balance = r["balance"]     or 0
            status  = "PAID" if r["is_paid"] == "Yes" else f"Outstanding £{balance:,.2f}"
            status_cls = "badge-paid" if r["is_paid"] == "Yes" else "badge-partial"
            rows_html += f"""
            <tr>
              <td style='font-size:11px;color:#94a3b8'>{r['location']}</td>
              <td style='font-weight:700'>{r['supplier_name']}</td>
              <td class='mono' style='font-size:12px'>{r['invoice_number'] or '—'}</td>
              <td class='mono'>£{r['gross_amount']:,.2f}</td>
              <td class='mono' style='color:#16a34a;font-weight:700'>£{paid:,.2f}</td>
              <td style='font-size:12px;color:#64748b'>{r['payment_method'] or '—'}</td>
              <td><span class='{status_cls}'>{status}</span></td>
            </tr>"""

    content = f"""
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>📋 Recent Payments</div>
      <a href='/invoices' class='btn-secondary'>← Back to Invoices</a>
    </div>
    <form method='GET' action='/invoices/recent-payments' class='card flex gap-3 items-end' style='margin-bottom:12px'>
      <div><label>Show</label><select name='scope' onchange='this.form.submit()'>{scope_opts}</select></div>
      <button type='submit' class='btn-secondary'>🔍 Filter</button>
    </form>
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead>
            <tr>
              <th>Store/Property</th><th>Supplier</th><th>Invoice No.</th>
              <th>Gross</th><th>Paid</th><th>Method</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows_html or '<tr><td colspan="7" style="text-align:center;padding:32px;color:#94a3b8">No payments recorded yet</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>"""
    return page("Recent Payments", content, user, "invoices")


_BAL_SQL = "COALESCE(gross_amount,0)-COALESCE(amount_paid,0)-COALESCE(credit_note,0)"


@router.get("/invoices/dd-collection", response_class=HTMLResponse)
def dd_collection(session: str | None = Cookie(default=None),
                  store: str = "", dd_date: str = "", show_stmt: str = "",
                  msg: str = "", msg_type: str = "success"):
    """Owner-only DD reconciliation, ONE STORE at a time (each store is a separate
    bank account with its own DD statements): pick a store, pick a statement date,
    see that store's invoices for it + total, add any missing credit, attach the
    statement, then mark the whole collection paid."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=DD+Collection+Check+is+owner-only&msg_type=error",
                                status_code=303)
    STORES = ["Uxbridge", "Newbury"]
    if store not in STORES:
        store = ""

    flash = ""
    if msg:
        colour = "#16a34a" if msg_type == "success" else "#dc2626"
        bg     = "#f0fdf4" if msg_type == "success" else "#fef2f2"
        flash = (f"<div style='background:{bg};border:1px solid {colour};color:{colour};"
                 f"border-radius:10px;padding:12px 16px;margin-bottom:12px;font-weight:700'>{msg}</div>")

    store_btns = ""
    for s in STORES:
        cls = "btn-primary" if store == s else "btn-secondary"
        store_btns += (f"<a href='/invoices/dd-collection?store={s}' class='{cls}' "
                       f"style='padding:8px 18px'>🏪 {s}</a> ")

    body = ""
    if not store:
        body = ("<div class='card' style='margin-top:14px;color:#64748b'>"
                "Pick a store above to reconcile its Direct Debit collections — each store has "
                "its own bank account and its own DD statements.</div>")
    else:
        dates = q(f"""SELECT dd_statement_date d, COUNT(*) n, SUM({_BAL_SQL}) tot
                      FROM supplier_invoices
                      WHERE store_name=? AND dd_statement_date IS NOT NULL AND is_paid!='Yes'
                      GROUP BY dd_statement_date ORDER BY dd_statement_date DESC""",
                  (store,), fetch=True) or []
        chips = ""
        for r in dates:
            selc = ("background:#0f2942;color:white" if r["d"] == dd_date
                    else "background:#ecfdf5;color:#047857;border:1px solid #6ee7b7")
            chips += (f"<a href='/invoices/dd-collection?store={store}&dd_date={r['d']}' "
                      f"style='{selc};border-radius:8px;padding:6px 12px;margin:3px;font-size:13px;"
                      f"font-weight:700;text-decoration:none;display:inline-block'>"
                      f"{fmt_uk_date(r['d'])} · {r['n']} inv · £{(r['tot'] or 0):,.2f}</a>")
        if not chips:
            chips = f"<span style='color:#94a3b8;font-size:13px'>No unpaid {store} invoices have a DD statement date.</span>"
        # Reconciled collections (a statement is on file) — clickable to review later
        recon = q("SELECT DISTINCT dd_date FROM dd_statements WHERE store_name=? ORDER BY dd_date DESC",
                  (store,), fetch=True) or []
        rchips = ""
        for r in recon:
            selc = ("background:#0f2942;color:white" if r["dd_date"] == dd_date
                    else "background:#f1f5f9;color:#475569;border:1px solid #cbd5e1")
            rchips += (f"<a href='/invoices/dd-collection?store={store}&dd_date={r['dd_date']}' "
                       f"style='{selc};border-radius:8px;padding:5px 11px;margin:3px;font-size:12px;"
                       f"font-weight:700;text-decoration:none;display:inline-block'>📄 {fmt_uk_date(r['dd_date'])}</a>")
        recon_html = (f"<div style='margin-top:12px;font-size:12px;font-weight:700;color:#64748b'>"
                      f"Reconciled (statement on file — click to review):</div>{rchips}") if rchips else ""
        picker = (f"<div class='card' style='margin-top:12px'>"
                  f"<div style='font-size:13px;font-weight:700;color:#334155;margin-bottom:8px'>"
                  f"{store} — pick a DD statement date to reconcile:</div>{chips}{recon_html}</div>")

        detail = ""
        if dd_date:
            rows = q(f"""SELECT seq_no, supplier_name, invoice_number, {_BAL_SQL} AS balance
                         FROM supplier_invoices
                         WHERE store_name=? AND dd_statement_date=? AND is_paid!='Yes'
                         ORDER BY supplier_name, seq_no""", (store, dd_date), fetch=True) or []
            total = round(sum(r["balance"] or 0 for r in rows), 2)
            sup_default = rows[0]["supplier_name"] if rows else ""
            tr = ""
            for r in rows:
                tr += (f"<tr><td class='mono' style='color:#94a3b8;font-size:12px'>{r['seq_no'] or ''}</td>"
                       f"<td style='font-weight:700'>{html.escape(r['supplier_name'] or '')}</td>"
                       f"<td class='mono' style='font-size:12px'>{html.escape(r['invoice_number'] or '—')}</td>"
                       f"<td class='mono' style='text-align:right;font-weight:700'>£{(r['balance'] or 0):,.2f}</td></tr>")

            st = q("SELECT dd_id, orig_name FROM dd_statements WHERE store_name=? AND dd_date=? ORDER BY dd_id DESC LIMIT 1",
                   (store, dd_date), fetch=True)
            if st:
                std = dict(st[0])
                stmt_html = (f"<div style='font-size:12px'>📄 DD statement attached: "
                             f"<a href='#' onclick=\"ddShowPdf('/invoices/dd-collection/statement/{std['dd_id']}');return false;\" "
                             f"style='color:#1e3a5f;font-weight:700'>{html.escape(std['orig_name'] or 'view')}</a> "
                             f"<span style='color:#94a3b8'>(opens side-by-side →)</span></div>")
            else:
                stmt_html = ("<form method='POST' action='/invoices/dd-collection/attach' "
                             "enctype='multipart/form-data' style='display:flex;gap:6px;align-items:center;flex-wrap:wrap'>"
                             f"<input type='hidden' name='store' value='{store}'>"
                             f"<input type='hidden' name='dd_date' value='{dd_date}'>"
                             "<span style='font-size:12px;color:#64748b'>Attach the DD statement:</span>"
                             "<input type='file' name='statement' accept='.pdf,.png,.jpg,.jpeg' style='font-size:12px'>"
                             "<button type='submit' class='btn-secondary' style='padding:3px 10px;font-size:11px'>📎 Attach</button></form>")

            if rows:
                _def_note = html.escape(f"Credit applied on DD {fmt_uk_date(dd_date)}, no CN document received, not queried")
                detail = f"""
                <div class='card' style='margin-top:14px;padding:0;overflow:hidden'>
                  <div style='padding:14px 18px;background:#0f2942;color:white;font-weight:700'>
                    {store} · DD Statement {fmt_uk_date(dd_date)} — {len(rows)} invoice(s)
                  </div>
                  <div style='overflow-x:auto'>
                    <table class='tbl'>
                      <thead><tr><th>Serial</th><th>Supplier</th><th>Invoice No.</th>
                        <th style='text-align:right'>Balance</th></tr></thead>
                      <tbody>{tr}</tbody>
                      <tfoot><tr style='background:#f0fdf4'>
                        <td colspan='3' style='text-align:right;font-weight:900'>App total for this collection:</td>
                        <td class='mono' style='text-align:right;font-weight:900;color:#047857'>£{total:,.2f}</td>
                      </tr></tfoot>
                    </table>
                  </div>
                  <div style='padding:12px 18px;border-top:1px solid #eef2f7'>{stmt_html}</div>
                  <div style='padding:12px 18px;background:#fffbeb;border-top:1px solid #fde68a'>
                    <div style='font-size:12px;font-weight:700;color:#92400e;margin-bottom:6px'>
                      Statement total lower than the app total? Add the missing credit (e.g. a credit the supplier
                      applied but you have no CN for) so it reconciles:</div>
                    <form method='POST' action='/invoices/dd-collection/add-credit'
                          style='display:flex;gap:6px;align-items:center;flex-wrap:wrap'>
                      <input type='hidden' name='store' value='{store}'>
                      <input type='hidden' name='dd_date' value='{dd_date}'>
                      <input type='text' name='supplier' value='{html.escape(sup_default)}' placeholder='Supplier'
                             style='font-size:12px;padding:3px 6px'>
                      <span style='font-size:12px'>Credit £</span>
                      <input type='number' step='0.01' name='amount' placeholder='67.20'
                             style='font-size:12px;padding:3px 6px;width:90px'>
                      <input type='text' name='note' value='{_def_note}'
                             style='font-size:12px;padding:3px 6px;flex:1;min-width:220px'>
                      <button type='submit' class='btn-secondary' style='padding:3px 10px;font-size:11px'>➕ Add credit</button>
                    </form>
                  </div>
                  <form method='POST' action='/invoices/dd-collection/mark-paid' style='padding:16px 18px;background:#f8fafc'>
                    <input type='hidden' name='store' value='{store}'>
                    <input type='hidden' name='dd_date' value='{dd_date}'>
                    <div class='text-xs font-bold text-slate-500 uppercase tracking-wide mb-3'>Mark this collection paid</div>
                    <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(160px,1fr))'>
                      <div><label>Bank debit date</label>
                        <input type='date' name='bank_debit_date' value='{dd_date}'></div>
                      <div><label>Amount the bank collected (£)</label>
                        <input type='number' step='0.01' id='ddActual' name='actual_amount' value='{total:.2f}' oninput='ddCalc()'></div>
                      <div><label>Difference vs app total</label>
                        <div id='ddDiff' style='font-weight:900;padding-top:9px'>£0.00</div></div>
                      <div style='grid-column:1/-1'><label>Note (explains any difference)</label>
                        <input type='text' name='note' placeholder='Optional'></div>
                    </div>
                    <div style='margin-top:12px'>
                      <button type='submit' class='btn-primary'
                        onclick="return confirm('Mark all {len(rows)} {store} invoice(s) on DD statement {fmt_uk_date(dd_date)} as paid?');">
                        ✅ Mark all {len(rows)} as paid
                      </button>
                    </div>
                  </form>
                </div>
                <script>
                function ddCalc(){{
                  var a=parseFloat(document.getElementById('ddActual').value||0);
                  var d=a-({total});
                  var el=document.getElementById('ddDiff');
                  el.textContent=(d>=0?'+':'-')+'£'+Math.abs(d).toFixed(2);
                  el.style.color=Math.abs(d)<0.005?'#16a34a':'#b45309';
                }}
                document.addEventListener('DOMContentLoaded',ddCalc);
                </script>"""
            else:
                # Reconciled (all paid) — read-only review, with the statement to view.
                paid = q(f"""SELECT seq_no, supplier_name, invoice_number, paid_date,
                                    COALESCE(amount_paid,0) AS amt
                             FROM supplier_invoices
                             WHERE store_name=? AND dd_statement_date=? AND is_paid='Yes'
                             ORDER BY supplier_name, seq_no""", (store, dd_date), fetch=True) or []
                if paid or st:
                    ptot    = round(sum(r["amt"] or 0 for r in paid), 2)
                    paid_on = fmt_uk_date(paid[0]["paid_date"]) if paid and paid[0]["paid_date"] else "—"
                    ptr = ""
                    for r in paid:
                        ptr += (f"<tr><td class='mono' style='color:#94a3b8;font-size:12px'>{r['seq_no'] or ''}</td>"
                                f"<td style='font-weight:700'>{html.escape(r['supplier_name'] or '')}</td>"
                                f"<td class='mono' style='font-size:12px'>{html.escape(r['invoice_number'] or '—')}</td>"
                                f"<td class='mono' style='text-align:right;font-weight:700'>£{(r['amt'] or 0):,.2f}</td></tr>")
                    stmt_view = (f"<a href='#' onclick=\"ddShowPdf('/invoices/dd-collection/statement/{dict(st[0])['dd_id']}');return false;\" "
                                 f"style='color:#1e3a5f;font-weight:700'>📄 View attached statement side-by-side "
                                 f"({html.escape(dict(st[0]).get('orig_name') or 'file')})</a>"
                                 if st else "<span style='color:#94a3b8;font-size:12px'>No statement attached.</span>")
                    detail = f"""
                    <div class='card' style='margin-top:14px;padding:0;overflow:hidden'>
                      <div style='padding:14px 18px;background:#334155;color:white;font-weight:700'>
                        ✅ Reconciled — {store} · DD {fmt_uk_date(dd_date)} · paid {paid_on}
                      </div>
                      <div style='overflow-x:auto'><table class='tbl'>
                        <thead><tr><th>Serial</th><th>Supplier</th><th>Invoice No.</th>
                          <th style='text-align:right'>Paid</th></tr></thead>
                        <tbody>{ptr}</tbody>
                        <tfoot><tr style='background:#f8fafc'>
                          <td colspan='3' style='text-align:right;font-weight:900'>Collected total:</td>
                          <td class='mono' style='text-align:right;font-weight:900'>£{ptot:,.2f}</td></tr></tfoot>
                      </table></div>
                      <div style='padding:12px 18px;border-top:1px solid #eef2f7'>{stmt_view}</div>
                    </div>"""
                else:
                    detail = ("<div class='card' style='margin-top:14px;color:#64748b'>"
                              f"Nothing to show for {store} DD {fmt_uk_date(dd_date)}.</div>")
        body = picker + detail

    # Side-by-side statement viewer (like the invoice PDF panel): opens the DD
    # statement on the right so it can be read while reconciling on the left.
    dd_panel = """
    <div id="ddPdfPanel" style="display:none;position:fixed;top:0;right:0;width:46%;height:100vh;
         background:#fff;box-shadow:-4px 0 20px rgba(0,0,0,.18);z-index:70;flex-direction:column">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#0f2942;color:#fff">
        <span style="font-weight:700;font-size:13px">&#128196; DD statement</span>
        <button onclick="ddClosePdf()" class="btn-secondary" style="padding:3px 12px;font-size:12px">&#10005; Close</button>
      </div>
      <iframe id="ddPdfFrame" src="" style="flex:1;width:100%;border:none"></iframe>
    </div>
    <script>
    function ddShowPdf(u){var p=document.getElementById('ddPdfPanel');document.getElementById('ddPdfFrame').src=u;p.style.display='flex';}
    function ddClosePdf(){var p=document.getElementById('ddPdfPanel');p.style.display='none';document.getElementById('ddPdfFrame').src='';}
    </script>"""
    # Auto-open the statement (side-by-side) right after it's attached.
    auto_open = ""
    if show_stmt and store and dd_date:
        _s = q("SELECT dd_id FROM dd_statements WHERE store_name=? AND dd_date=? ORDER BY dd_id DESC LIMIT 1",
               (store, dd_date), fetch=True)
        if _s:
            auto_open = (f"<script>document.addEventListener('DOMContentLoaded',function(){{"
                         f"ddShowPdf('/invoices/dd-collection/statement/{dict(_s[0])['dd_id']}');}});</script>")

    content = f"""
    {flash}
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>🏦 DD Collection Check</div>
      <a href='/invoices' class='btn-secondary'>← Back to Invoices</a>
    </div>
    <div class='card' style='margin-top:12px'>
      <div style='font-size:13px;font-weight:700;color:#334155;margin-bottom:8px'>
        Which store's DD statement are you reconciling?</div>
      {store_btns}
    </div>
    {body}
    {dd_panel}{auto_open}"""
    return page("DD Collection Check", content, user, "invoices")


@router.post("/invoices/dd-collection/mark-paid")
async def dd_collection_mark_paid(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Owner+only&msg_type=error", status_code=303)

    form      = await request.form()
    store     = (form.get("store") or "").strip()
    dd_date   = (form.get("dd_date") or "").strip()
    bank_date = (form.get("bank_debit_date") or "").strip() or dd_date
    note      = (form.get("note") or "").strip()
    try:
        actual = float(form.get("actual_amount") or 0)
    except (TypeError, ValueError):
        actual = 0.0
    if not dd_date:
        return RedirectResponse("/invoices/dd-collection", status_code=303)

    # Only this store's invoices for the statement date (each store = its own account)
    where = "dd_statement_date=? AND is_paid!='Yes'"
    params = [dd_date]
    if store:
        where += " AND store_name=?"; params.append(store)
    rows = q(f"""SELECT invoice_id, comments, {_BAL_SQL} AS balance
                 FROM supplier_invoices WHERE {where}""",
             tuple(params), fetch=True) or []
    total = round(sum(r["balance"] or 0 for r in rows), 2)
    diff  = round(actual - total, 2) if actual else 0.0

    # Build a single explanatory note (recorded on each invoice) when the bank
    # figure differs from the invoice total, or when the user typed one.
    note_suffix = ""
    if note or diff:
        parts = [f"DD {fmt_uk_date(dd_date)} reconciled"]
        if actual:
            parts.append(f"collected £{actual:,.2f} (diff £{diff:+.2f})")
        if note:
            parts.append(note)
        note_suffix = " | " + "; ".join(parts)

    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        new_comments = (r["comments"] or "")
        if note_suffix:
            new_comments = (new_comments + note_suffix).strip(" |")
        q("""UPDATE supplier_invoices SET is_paid='Yes', paid_date=?,
                amount_paid=gross_amount,
                payment_method=COALESCE(NULLIF(payment_method,''),'Direct Debit'),
                comments=?, updated_by=?, updated_at=?
             WHERE invoice_id=?""",
          (bank_date, new_comments, user.get("username", ""), now_ts, r["invoice_id"]))

    from urllib.parse import quote as urlquote
    msg = (f"Marked {len(rows)} {store} invoice(s) paid for DD statement {fmt_uk_date(dd_date)} "
           f"(paid {fmt_uk_date(bank_date)}).")
    if diff:
        msg += f" Difference of £{diff:+.2f} noted."
    return RedirectResponse(f"/invoices/dd-collection?store={store}&msg={urlquote(msg)}&msg_type=success",
                            status_code=303)


@router.post("/invoices/dd-collection/add-credit")
async def dd_add_credit(request: Request, session: str | None = Cookie(default=None)):
    """Add a missing credit as a negative-gross line, tagged to a store's DD statement,
    so the collection reconciles. Editable later if the real credit note surfaces."""
    from urllib.parse import quote as urlquote
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices/dd-collection?msg=Owner+only&msg_type=error", status_code=303)
    form     = await request.form()
    store    = (form.get("store") or "").strip()
    dd_date  = (form.get("dd_date") or "").strip()
    supplier = (form.get("supplier") or "").strip()
    note     = (form.get("note") or "").strip()
    try:    amount = abs(float(form.get("amount") or 0))
    except (TypeError, ValueError): amount = 0.0
    back = f"/invoices/dd-collection?store={store}&dd_date={dd_date}"
    if not (store and dd_date and supplier and amount):
        return RedirectResponse(back + "&msg=" + urlquote("Enter a supplier and a credit amount") + "&msg_type=error",
                                status_code=303)
    mx  = q("SELECT MAX(seq_no) AS m FROM supplier_invoices", (), fetch=True)
    seq = ((dict(mx[0]).get("m") or 0) + 1) if mx else 1
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    q("""INSERT INTO supplier_invoices
            (seq_no, supplier_name, store_name, invoice_date, gross_amount, vat_amount,
             net_amount, dd_statement_date, payment_method, is_paid, comments,
             approval_status, submitted_by, created_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (seq, supplier, store, dd_date, -amount, 0, -amount, dd_date, "Direct Debit",
       "No", note or None, "approved", user.get("username", ""), now_ts))
    return RedirectResponse(back + "&msg=" + urlquote(f"Added credit -£{amount:.2f} ({supplier}) as serial {seq}"),
                            status_code=303)


@router.post("/invoices/dd-collection/attach")
async def dd_attach(request: Request, session: str | None = Cookie(default=None)):
    """Attach a store's DD collection statement (the supplier's DD advice) to a date."""
    from urllib.parse import quote as urlquote
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices/dd-collection?msg=Owner+only&msg_type=error", status_code=303)
    form    = await request.form()
    store   = (form.get("store") or "").strip()
    dd_date = (form.get("dd_date") or "").strip()
    f       = form.get("statement")
    back    = f"/invoices/dd-collection?store={store}&dd_date={dd_date}"
    if not (store and dd_date and f and hasattr(f, "filename") and f.filename):
        return RedirectResponse(back + "&msg=" + urlquote("No file chosen") + "&msg_type=error", status_code=303)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext  = os.path.splitext(f.filename)[1].lower()
    full = os.path.join(UPLOAD_DIR, f"dd_{uuid.uuid4().hex}{ext}")
    with open(full, "wb") as out:
        out.write(await f.read())
    q("""INSERT INTO dd_statements (store_name, dd_date, file_path, orig_name, uploaded_by)
         VALUES (?,?,?,?,?)""", (store, dd_date, full, f.filename, user.get("username", "")))
    return RedirectResponse(back + "&show_stmt=1&msg=" + urlquote("DD statement attached"), status_code=303)


@router.get("/invoices/dd-collection/statement/{dd_id}")
def dd_statement_serve(dd_id: int, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return HTMLResponse("<p>Owner only</p>", status_code=403)
    rows = q("SELECT file_path FROM dd_statements WHERE dd_id=?", (dd_id,), fetch=True)
    if not rows or not rows[0]["file_path"] or not os.path.exists(rows[0]["file_path"]):
        return HTMLResponse("<p>Statement not found</p>", status_code=404)
    path = rows[0]["file_path"]; ext = os.path.splitext(path)[1].lower()
    mt = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=mt)


@router.get("/invoices/accountant-batch", response_class=HTMLResponse)
def accountant_batch(session: str | None = Cookie(default=None),
                     scope: str = "", date_from: str = "", date_to: str = "",
                     msg: str = "", msg_type: str = "success"):
    """Owner-only: mark a batch of invoices as sent to the accountant in one go.
    Covers both retail stores and properties; lists invoices not yet sent."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Send+to+Accountant+is+owner-only&msg_type=error",
                                status_code=303)

    # Only build the list once the owner has DELIBERATELY chosen a scope and hit
    # Filter — landing on the page shows nothing, so nothing can be marked by
    # accident. scope="" is the "— choose —" prompt; scope="ALL" = Everything.
    filtered    = bool(scope)
    want_retail = scope in ("ALL", "Uxbridge", "Newbury")
    want_prop   = scope in ("ALL", "Property")
    date_conds, date_params = [], []
    if date_from: date_conds.append("invoice_date>=?"); date_params.append(date_from)
    if date_to:   date_conds.append("invoice_date<=?"); date_params.append(date_to)
    date_sql = (" AND " + " AND ".join(date_conds)) if date_conds else ""

    rows = []
    if filtered and want_retail:
        # Exclude demand notes / pro-formas — they aren't VAT invoices yet.
        rc, rp = ["accountant_sent_date IS NULL",
                  "(awaiting_invoice IS NULL OR awaiting_invoice!='Yes')"], []
        if scope in ("Uxbridge", "Newbury"):
            rc.append("store_name=?"); rp.append(scope)
        for r in (q(f"""SELECT invoice_id, seq_no, store_name loc, supplier_name,
                               invoice_number, invoice_date, gross_amount
                        FROM supplier_invoices WHERE {' AND '.join(rc)}{date_sql}""",
                    rp + date_params, fetch=True) or []):
            rows.append(("supplier", r))
    if filtered and want_prop:
        for r in (q(f"""SELECT invoice_id, seq_no, property_name loc, supplier_name,
                               invoice_number, invoice_date, gross_amount
                        FROM property_invoices
                        WHERE accountant_sent_date IS NULL
                          AND (awaiting_invoice IS NULL OR awaiting_invoice!='Yes'){date_sql}""",
                    date_params, fetch=True) or []):
            rows.append(("property", r))

    rows.sort(key=lambda sr: (sr[1]["invoice_date"] or "", sr[1]["seq_no"] or 0), reverse=True)
    total_n = len(rows)
    total_t = sum((r["gross_amount"] or 0) for _, r in rows)
    rows = rows[:500]

    store_opts = "".join(
        f"<option value='{v}' {'selected' if scope==v else ''}>{lbl}</option>"
        for v, lbl in [("", "— choose —"), ("Uxbridge", "Uxbridge"),
                       ("Newbury", "Newbury"), ("Property", "Properties"), ("ALL", "Everything")])

    tr = ""
    for src, r in rows:
        tr += (f"<tr><td><input type='checkbox' name='ids' value='{src}:{r['invoice_id']}' checked class='rowchk'></td>"
               f"<td class='mono' style='color:#94a3b8;font-size:12px'>{r['seq_no'] or ''}</td>"
               f"<td style='font-size:12px'>{r['loc']}</td>"
               f"<td style='font-weight:700'>{r['supplier_name']}</td>"
               f"<td class='mono' style='font-size:12px'>{r['invoice_number'] or '—'}</td>"
               f"<td class='mono' style='font-size:12px;color:#64748b'>{fmt_uk_date(r['invoice_date'])}</td>"
               f"<td class='mono' style='text-align:right'>£{(r['gross_amount'] or 0):,.2f}</td></tr>")

    capped = ("<div style='color:#b45309;font-size:12px;margin:6px 0'>Showing the first 500 — "
              "narrow with the filters above to see the rest.</div>") if total_n > 500 else ""
    tot = {"n": total_n, "t": total_t}

    flash = ""
    if msg:
        colour = "#16a34a" if msg_type == "success" else "#dc2626"
        bg     = "#f0fdf4" if msg_type == "success" else "#fef2f2"
        flash = (f"<div style='background:{bg};border:1px solid {colour};color:{colour};"
                 f"border-radius:10px;padding:12px 16px;margin-bottom:12px;font-weight:700'>{msg}</div>")

    today = datetime.now().strftime("%Y-%m-%d")
    if not filtered:
        table_and_form = ("<div class='card' style='margin-top:12px;color:#64748b'>"
                          "👆 Choose a <b>store / scope</b> above (and, if you like, a date range), then press "
                          "<b>🔍 Filter</b> to see what's not yet sent. Nothing is listed until you do — so "
                          "nothing can be marked as sent by accident.</div>")
    elif rows:
        table_and_form = f"""
        <form method='POST' action='/invoices/accountant-batch/mark'>
          <div class='card' style='display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin-top:12px'>
            <div><label>Date sent to accountant</label>
              <input type='date' name='sent_date' value='{today}' required></div>
            <button type='submit' class='btn-primary'
              onclick="return confirm('Mark all ticked invoices as sent to the accountant?');">
              📨 Mark ticked invoices as sent
            </button>
            <span style='color:#64748b;font-size:13px'>{tot['n']} not yet sent · total £{tot['t']:,.2f}</span>
          </div>
          {capped}
          <div class='card' style='padding:0;overflow:hidden;margin-top:12px'>
            <div style='overflow-x:auto'>
              <table class='tbl'>
                <thead><tr>
                  <th><input type='checkbox' id='chkAll' checked title='Select all'></th>
                  <th>Serial</th><th>Store/Property</th><th>Supplier</th><th>Invoice No.</th>
                  <th>Inv. Date</th><th style='text-align:right'>Gross</th>
                </tr></thead>
                <tbody>{tr}</tbody>
              </table>
            </div>
          </div>
        </form>
        <script>
          document.getElementById('chkAll').addEventListener('change', function() {{
            document.querySelectorAll('.rowchk').forEach(c => c.checked = this.checked);
          }});
        </script>"""
    else:
        table_and_form = ("<div class='card' style='margin-top:12px;color:#64748b'>"
                          "No invoices match — nothing outstanding to send for this selection. ✅</div>")

    content = f"""
    {flash}
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>📨 Send to Accountant</div>
      <div class='flex gap-2'>
        <a href='/invoices/accountant-sent' class='btn-secondary'>📋 View sent history</a>
        <a href='/invoices' class='btn-secondary'>← Back to Invoices</a>
      </div>
    </div>
    <form method='GET' action='/invoices/accountant-batch' class='card flex flex-wrap gap-3 items-end' style='margin-top:12px'>
      <div><label>Ledger</label><select name='scope'>{store_opts}</select></div>
      <div><label>Invoice date from</label><input type='date' name='date_from' value='{date_from}'></div>
      <div><label>Invoice date to</label><input type='date' name='date_to' value='{date_to}'></div>
      <button type='submit' class='btn-secondary'>🔍 Filter</button>
    </form>
    {table_and_form}"""
    return page("Send to Accountant", content, user, "invoices")


@router.post("/invoices/accountant-batch/mark")
async def accountant_batch_mark(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Owner+only&msg_type=error", status_code=303)

    form = await request.form()
    sent_date = (form.get("sent_date") or "").strip()
    # Checkbox values are "src:id" so the two tables (which have independent
    # invoice_id sequences) can't be confused.
    supplier_ids, property_ids = [], []
    for raw in form.getlist("ids"):
        src, _, sid = str(raw).partition(":")
        if not sid.isdigit():
            continue
        (supplier_ids if src == "supplier" else property_ids).append(int(sid))
    from urllib.parse import quote as urlquote
    if not sent_date or not (supplier_ids or property_ids):
        return RedirectResponse("/invoices/accountant-batch?msg=Pick+a+date+and+at+least+one+invoice&msg_type=error",
                                status_code=303)

    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_name = user.get("username", "")
    for tbl, id_list in [("supplier_invoices", supplier_ids), ("property_invoices", property_ids)]:
        if id_list:
            ph = ",".join("?" * len(id_list))
            q(f"""UPDATE {tbl} SET accountant_sent_date=?, updated_by=?, updated_at=?
                  WHERE invoice_id IN ({ph})""",
              [sent_date, user_name, now_ts] + id_list)
    n = len(supplier_ids) + len(property_ids)
    msg = f"Marked {n} invoice(s) as sent to the accountant on {fmt_uk_date(sent_date)}."
    return RedirectResponse(f"/invoices/accountant-batch?msg={urlquote(msg)}&msg_type=success",
                            status_code=303)


@router.get("/invoices/accountant-sent", response_class=HTMLResponse)
def accountant_sent(session: str | None = Cookie(default=None), sent_date: str = "",
                    msg: str = "", msg_type: str = "success"):
    """Owner-only report of what HAS been sent to the accountant, grouped by the
    date sent. Pick a date to see every invoice in that batch."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Owner+only&msg_type=error", status_code=303)

    if sent_date:
        # True batch count/total (not limited by the display cap below).
        agg = q("""SELECT COUNT(*) n, COALESCE(SUM(gross_amount),0) t FROM (
                     SELECT gross_amount FROM supplier_invoices WHERE accountant_sent_date=?
                     UNION ALL
                     SELECT gross_amount FROM property_invoices WHERE accountant_sent_date=?
                   )""", (sent_date, sent_date), fetch=True)[0]
        true_count, total = agg["n"], agg["t"]
        rows = q("""
            SELECT 'Retail' src, store_name loc, seq_no, supplier_name, invoice_number,
                   invoice_date, gross_amount
            FROM supplier_invoices WHERE accountant_sent_date=?
            UNION ALL
            SELECT 'Property', property_name, seq_no, supplier_name, invoice_number,
                   invoice_date, gross_amount
            FROM property_invoices WHERE accountant_sent_date=?
            ORDER BY loc, supplier_name LIMIT 1000
        """, (sent_date, sent_date), fetch=True) or []
        capped_note = (f"<div style='color:#b45309;font-size:12px;padding:8px 18px'>"
                       f"Showing the first 1,000 of {true_count:,} — the batch total above covers all of them."
                       f"</div>") if true_count > len(rows) else ""
        tr = "".join(
            f"<tr><td class='mono' style='color:#94a3b8;font-size:12px'>{r['seq_no'] or ''}</td>"
            f"<td style='font-size:12px'>{r['loc']}</td>"
            f"<td style='font-weight:700'>{r['supplier_name']}</td>"
            f"<td class='mono' style='font-size:12px'>{r['invoice_number'] or '—'}</td>"
            f"<td class='mono' style='font-size:12px;color:#64748b'>{fmt_uk_date(r['invoice_date'])}</td>"
            f"<td class='mono' style='text-align:right'>£{(r['gross_amount'] or 0):,.2f}</td></tr>"
            for r in rows)
        body = f"""
        <div class='card' style='margin-top:14px;padding:0;overflow:hidden'>
          <div style='padding:14px 18px;background:#0f2942;color:white;font-weight:700'>
            Sent {fmt_uk_date(sent_date)} — {true_count:,} invoice(s)
          </div>
          {capped_note}
          <div style='overflow-x:auto'>
            <table class='tbl'>
              <thead><tr><th>Serial</th><th>Store/Property</th><th>Supplier</th>
                <th>Invoice No.</th><th>Inv. Date</th><th style='text-align:right'>Gross</th></tr></thead>
              <tbody>{tr}</tbody>
              <tfoot><tr style='background:#f0fdf4'>
                <td colspan='5' style='text-align:right;font-weight:900'>Batch total:</td>
                <td class='mono' style='text-align:right;font-weight:900;color:#047857'>£{total:,.2f}</td>
              </tr></tfoot>
            </table>
          </div>
        </div>"""
        # A combined PDF per STORE (separate companies), plus ONE for ALL
        # properties (done together), each in invoice-date order.
        from urllib.parse import quote as _q
        _stores = q("""SELECT DISTINCT store_name s FROM supplier_invoices
                       WHERE accountant_sent_date=? AND store_name IS NOT NULL AND store_name<>''
                       ORDER BY store_name""", (sent_date,), fetch=True) or []
        _hasprop = q("SELECT 1 FROM property_invoices WHERE accountant_sent_date=? LIMIT 1",
                     (sent_date,), fetch=True)
        _dl = "".join(
            f"<a href='/invoices/combined-pdf?sent_date={sent_date}&loc={_q(s['s'])}' "
            f"class='btn-primary dlbtn' style='font-size:12px'>⬇️ {s['s']} PDF</a>" for s in _stores)
        if _hasprop:
            _dl += (f"<a href='/invoices/combined-pdf?sent_date={sent_date}&loc=Properties' "
                    f"class='btn-primary dlbtn' style='font-size:12px'>⬇️ Properties PDF</a>")
        _cmp = ("<label style='font-size:12px;color:#475569;display:flex;align-items:center;gap:4px'>"
                "<input type='checkbox' id='cmp'> 🗜️ Smaller file (for email)</label>")
        _unsend = ("<form method='POST' action='/invoices/accountant-unsend' style='display:inline' "
                   "onsubmit=\"return confirm('Un-mark ALL invoices in this batch as sent? They go back to "
                   "not-yet-sent. Nothing else changes.');\">"
                   f"<input type='hidden' name='sent_date' value='{sent_date}'>"
                   "<button type='submit' class='btn-secondary' style='font-size:12px;color:#dc2626'>"
                   "↩ Un-mark this batch as sent</button></form>")
        head_extra = _cmp + _dl + _unsend + "<a href='/invoices/accountant-sent' class='btn-secondary'>↩ All batches</a>"
    else:
        batches = q("""
            SELECT d, SUM(n) n, SUM(t) t FROM (
              SELECT accountant_sent_date d, COUNT(*) n, COALESCE(SUM(gross_amount),0) t
              FROM supplier_invoices WHERE accountant_sent_date IS NOT NULL GROUP BY d
              UNION ALL
              SELECT accountant_sent_date d, COUNT(*) n, COALESCE(SUM(gross_amount),0) t
              FROM property_invoices WHERE accountant_sent_date IS NOT NULL GROUP BY d
            ) GROUP BY d ORDER BY d DESC
        """, (), fetch=True) or []
        rowshtml = "".join(
            f"<tr style='cursor:pointer' onclick=\"window.location='/invoices/accountant-sent?sent_date={b['d']}'\">"
            f"<td style='font-weight:700'>{fmt_uk_date(b['d'])}</td>"
            f"<td class='mono' style='text-align:right'>{b['n']}</td>"
            f"<td class='mono' style='text-align:right'>£{(b['t'] or 0):,.2f}</td></tr>"
            for b in batches)
        body = f"""
        <div class='card' style='margin-top:12px;padding:0;overflow:hidden'>
          <div style='overflow-x:auto'>
            <table class='tbl'>
              <thead><tr><th>Date sent</th><th style='text-align:right'>Invoices</th>
                <th style='text-align:right'>Total</th></tr></thead>
              <tbody>{rowshtml or "<tr><td colspan='3' style='text-align:center;padding:24px;color:#94a3b8'>Nothing sent yet</td></tr>"}</tbody>
            </table>
          </div>
        </div>
        <div style='color:#94a3b8;font-size:12px;margin-top:6px'>Click a date to see the invoices in that batch.</div>"""
        head_extra = ""

    _flash = ""
    if msg:
        _flash = f"<div class='flash-{'success' if msg_type == 'success' else 'error'}'>{html.escape(msg)}</div>"
    content = f"""
    {_flash}
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>📋 Sent to Accountant — history</div>
      <div class='flex gap-2'>
        {head_extra}
        <a href='/invoices/accountant-batch' class='btn-secondary'>📨 Send more</a>
        <a href='/invoices' class='btn-secondary'>← Back to Invoices</a>
      </div>
    </div>
    {body}
    <script>
    (function(){{
      var cb=document.getElementById('cmp'); if(!cb) return;
      var btns=document.querySelectorAll('.dlbtn');
      btns.forEach(function(b){{ b.dataset.base=b.getAttribute('href'); }});
      cb.addEventListener('change', function(){{
        btns.forEach(function(b){{ b.setAttribute('href', b.dataset.base + (cb.checked?'&compress=1':'')); }});
      }});
    }})();
    </script>"""
    return page("Sent to Accountant — history", content, user, "invoices")


@router.post("/invoices/accountant-unsend")
def accountant_unsend(sent_date: str = Form(""), session: str | None = Cookie(default=None)):
    """Owner-only: clear the 'sent to accountant' date for every invoice in a
    batch, putting them back to 'not yet sent' — e.g. after a test run, or to fix
    a mis-marked batch. Non-destructive: only the sent flag is cleared."""
    redir, user = require_login(session)
    if redir: return redir
    from urllib.parse import quote as urlquote
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Owner+only&msg_type=error", status_code=303)
    if not sent_date:
        return RedirectResponse("/invoices/accountant-sent", status_code=303)
    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    who = user.get("username", "")
    n = 0
    for tbl in ("supplier_invoices", "property_invoices"):
        cnt = q(f"SELECT COUNT(*) c FROM {tbl} WHERE accountant_sent_date=?", (sent_date,), fetch=True)
        n += (cnt[0]["c"] if cnt else 0)
        q(f"UPDATE {tbl} SET accountant_sent_date=NULL, updated_by=?, updated_at=? WHERE accountant_sent_date=?",
          (who, now_ts, sent_date))
    return RedirectResponse(
        f"/invoices/accountant-sent?msg={urlquote(f'Un-marked {n} invoice(s) — back to not-yet-sent.')}&msg_type=success",
        status_code=303)


@router.get("/invoices/combined-pdf")
def combined_pdf(sent_date: str = "", loc: str = "", compress: str = "", session: str | None = Cookie(default=None)):
    """Owner-only: merge every attached invoice PDF in one accountant batch (a
    given accountant_sent_date, retail + property) into a single PDF to email —
    replaces the manual chore of combining 30-80 files by hand. Image scans are
    converted to PDF pages; unreadable/missing files are skipped."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Owner+only&msg_type=error", status_code=303)
    if not sent_date:
        return HTMLResponse("<p>No batch date given.</p>", status_code=400)

    # This batch, scoped to the chosen company, in invoice-DATE order. Each store
    # is its own file; ALL properties combine into one "Properties" file.
    if loc == "Properties":
        rows = q("""SELECT invoice_date, pdf_path FROM property_invoices
                    WHERE accountant_sent_date=? ORDER BY invoice_date, seq_no""",
                 (sent_date,), fetch=True) or []
    elif loc:
        rows = q("""SELECT invoice_date, pdf_path FROM supplier_invoices
                    WHERE accountant_sent_date=? AND store_name=? ORDER BY invoice_date, seq_no""",
                 (sent_date, loc), fetch=True) or []
    else:
        rows = q("""SELECT invoice_date, pdf_path FROM (
                      SELECT invoice_date, seq_no, pdf_path FROM supplier_invoices WHERE accountant_sent_date=?
                      UNION ALL
                      SELECT invoice_date, seq_no, pdf_path FROM property_invoices WHERE accountant_sent_date=?
                    ) ORDER BY invoice_date, seq_no""", (sent_date, sent_date), fetch=True) or []
    paths = [r["pdf_path"] for r in rows if r["pdf_path"]]

    # Invoice-period label for the filename: a single month (Jun2026) or a range
    # (May2026-Jun2026) when the batch spans months (late "filed at start of…" ones).
    def _mlabel(d):
        try:
            return datetime.strptime(str(d)[:7], "%Y-%m").strftime("%b%Y")
        except Exception:
            return ""
    _dts = [r["invoice_date"] for r in rows if r["invoice_date"]]
    if _dts:
        _lo, _hi = _mlabel(min(_dts)), _mlabel(max(_dts))
        month_part = _lo if _lo == _hi else f"{_lo}-{_hi}"
    else:
        month_part = ""

    MAXN = 400
    if len(paths) > MAXN:
        return HTMLResponse(f"<p>This batch has {len(paths)} attached documents — too many to combine "
                            f"into one file. Please split it into smaller batches.</p>", status_code=400)
    if not paths:
        return HTMLResponse("<p>No invoice PDFs are attached in this batch, so there's nothing to "
                            "combine. Attach the invoice scans first.</p>", status_code=404)

    _img_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")

    # Always build the clean lossless merge first.
    from pypdf import PdfWriter, PdfReader
    writer = PdfWriter()
    for p in paths:
        if not os.path.exists(p):
            continue
        ext = os.path.splitext(p)[1].lower()
        try:
            if ext == ".pdf":
                for pg in PdfReader(p).pages:
                    writer.add_page(pg)
            elif ext in _img_exts:
                from PIL import Image
                buf = io.BytesIO()
                Image.open(p).convert("RGB").save(buf, "PDF")
                buf.seek(0)
                for pg in PdfReader(buf).pages:
                    writer.add_page(pg)
        except Exception:
            continue
    if len(writer.pages) == 0:
        return HTMLResponse("<p>Could not read any of the attached documents in this batch.</p>", status_code=404)
    lb = io.BytesIO(); writer.write(lb)
    data, sfx = lb.getvalue(), ""

    if compress == "1":
        # Also build a rasterised/JPEG version, but use it ONLY if it's actually
        # smaller — for already-small/text PDFs, rasterising can be bigger, so we
        # never hand back something larger than the clean merge.
        try:
            from PIL import Image
            import pypdfium2 as pdfium
            from reportlab.pdfgen import canvas
            from reportlab.lib.utils import ImageReader
            DPI = 144.0
            def _jpeg_bytes(pil):
                pil = pil.convert("RGB"); pil.thumbnail((2000, 2000))
                b = io.BytesIO(); pil.save(b, "JPEG", quality=55, optimize=True)
                return b.getvalue()
            jpegs = []
            for p in paths:
                if not os.path.exists(p):
                    continue
                ext = os.path.splitext(p)[1].lower()
                try:
                    if ext == ".pdf":
                        doc = pdfium.PdfDocument(p)
                        for i in range(len(doc)):
                            jpegs.append(_jpeg_bytes(doc[i].render(scale=2.0).to_pil()))
                        doc.close()
                    elif ext in _img_exts:
                        jpegs.append(_jpeg_bytes(Image.open(p)))
                except Exception:
                    continue
            if jpegs:
                cbuf = io.BytesIO(); cv = canvas.Canvas(cbuf)
                for jb in jpegs:
                    ir = ImageReader(io.BytesIO(jb)); iw, ih = ir.getSize()
                    pw, ph = iw * 72.0 / DPI, ih * 72.0 / DPI
                    cv.setPageSize((pw, ph)); cv.drawImage(ir, 0, 0, width=pw, height=ph); cv.showPage()
                cv.save(); cdata = cbuf.getvalue()
                if cdata and len(cdata) < len(data):
                    data, sfx = cdata, "_compressed"
        except Exception:
            pass  # any failure → keep the clean lossless version

    _locpart = loc.replace(' ', '_').replace('/', '-') if loc else "Batch"
    _mpart = f"_{month_part}" if month_part else ""
    fn = f"Accountant_{_locpart}{_mpart}_sent{sent_date}{sfx}.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fn}"})


@router.get("/invoices/reports", response_class=HTMLResponse)
def reports(session: str | None = Cookie(default=None),
            report: str = "supplier", store: str = "Both", supplier: str = "",
            date_from: str = "", date_to: str = "", due_days: str = "14",
            exclude_dd: str = "", sort: str = "", comment: str = "", run: str = "", export: str = ""):
    """Owner-only flexible reporting. The chosen report decides which filters
    apply (e.g. Overdue ignores the supplier box), and results carry totals."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Reports+is+owner-only&msg_type=error", status_code=303)

    REPORTS = [("supplier", "Supplier statement"), ("overdue", "Overdue / due window"),
               ("upcoming", "Upcoming dues"), ("period", "Period / quarterly"),
               ("paid", "Paid in a period"), ("unpaid", "Unpaid (all outstanding)"),
               ("spend", "Spend per supplier (YTD)"), ("comment", "Comment search")]
    labels = dict(REPORTS)
    if report not in labels:
        report = "supplier"

    # ── Build the query for the chosen report — each uses only its own filters ──
    conds, params = [], []
    if store in ("Uxbridge", "Newbury"):
        conds.append("store_name=?"); params.append(store)
    if exclude_dd == "1":
        conds.append("COALESCE(payment_method,'')!='Direct Debit'")
    date_col = "invoice_date"

    if report == "supplier":
        if supplier.strip():
            conds.append("supplier_name=?"); params.append(supplier.strip())
        if date_from: conds.append("invoice_date>=?"); params.append(date_from)
        if date_to:   conds.append("invoice_date<=?"); params.append(date_to)
    elif report == "overdue":
        conds.append("is_paid!='Yes'")
        conds.append("due_date IS NOT NULL AND due_date<>''")
        # Upper bound: an explicit "Date to" (owner's choice) overrides the
        # relative "due within N days" window; blank dates = catch everything.
        if date_to:
            conds.append("due_date<=?"); params.append(date_to)
        else:
            try: n = int(due_days)
            except (TypeError, ValueError): n = 0
            cutoff = (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")
            conds.append("due_date<=?"); params.append(cutoff)
        if date_from:
            conds.append("due_date>=?"); params.append(date_from)
        date_col = "due_date"
    elif report == "period":
        if date_from: conds.append("invoice_date>=?"); params.append(date_from)
        if date_to:   conds.append("invoice_date<=?"); params.append(date_to)
    elif report == "spend":
        # Total spend per supplier over a period (leave dates blank = all time).
        if date_from: conds.append("invoice_date>=?"); params.append(date_from)
        if date_to:   conds.append("invoice_date<=?"); params.append(date_to)
    elif report == "paid":
        conds.append("is_paid='Yes'")
        if date_from: conds.append("paid_date>=?"); params.append(date_from)
        if date_to:   conds.append("paid_date<=?"); params.append(date_to)
        date_col = "paid_date"
    elif report == "unpaid":
        conds.append("is_paid!='Yes'")
        date_col = "due_date"
    elif report == "upcoming":
        if supplier.strip():
            conds.append("supplier_name=?"); params.append(supplier.strip())
        try: n = int(due_days)
        except (TypeError, ValueError): n = 30
        cutoff = (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")
        conds.append("is_paid!='Yes'")
        conds.append("due_date IS NOT NULL AND due_date<>'' AND due_date<=?"); params.append(cutoff)
        date_col = "due_date"
    elif report == "comment":
        # Match on the comment (and store) only — NOT dates. These "filed at the
        # start of…" invoices are deliberately dated OUTSIDE the filed month, so a
        # date filter would wrongly hide them.
        if comment.strip():
            conds.append("comments LIKE ?"); params.append(f"%{comment.strip()}%")

    # Sort: default sensibly per report, user-overridable; group by store first when "Both".
    if not sort:
        sort = {"supplier": "invdate", "overdue": "duedate", "upcoming": "duedate",
                "period": "supplier", "paid": "invdate", "unpaid": "duedate",
                "comment": "invdate"}.get(report, "supplier")
    sort_col = {"supplier": "supplier_name", "invdate": "invoice_date",
                "duedate": "due_date", "amount": "gross_amount"}.get(sort, "supplier_name")
    grouped = (store == "Both")
    order = f"ORDER BY {'store_name, ' if grouped else ''}{sort_col}, invoice_date"
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    do_run = (run == "1" or export == "csv")
    is_agg = (report == "spend")           # one row per supplier, not per invoice
    agg = []
    if is_agg:
        agg = (q(f"""SELECT supplier_name,
                            COUNT(*)                    AS c,
                            COALESCE(SUM(gross_amount),0) AS g,
                            COALESCE(SUM(vat_amount),0)   AS v,
                            COALESCE(SUM(net_amount),0)   AS n
                     FROM supplier_invoices {where}
                     GROUP BY supplier_name
                     ORDER BY SUM(gross_amount) DESC""",
                 tuple(params), fetch=True) or []) if do_run else []
        rows = []
        tot_g = round(sum(r["g"] or 0 for r in agg), 2)
        tot_v = round(sum(r["v"] or 0 for r in agg), 2)
        tot_n = round(sum(r["n"] or 0 for r in agg), 2)
    else:
        rows = (q(f"""SELECT invoice_id, seq_no, supplier_name, store_name, invoice_number, invoice_date,
                             due_date, paid_date, gross_amount, vat_amount, net_amount, is_paid
                      FROM supplier_invoices {where}
                      {order}""", tuple(params), fetch=True) or []) if do_run else []
        tot_g = round(sum(r["gross_amount"] or 0 for r in rows), 2)
        tot_v = round(sum(r["vat_amount"]   or 0 for r in rows), 2)
        tot_n = round(sum(r["net_amount"]   or 0 for r in rows), 2)

    # ── CSV (Excel) export ──
    if export == "csv":
        import csv
        buf = io.StringIO(); w = csv.writer(buf)
        if is_agg:
            w.writerow(["Supplier", "Invoices", "Amount", "VAT", "Net"])
            for r in agg:
                w.writerow([r["supplier_name"], r["c"], r["g"], r["v"], r["n"]])
            w.writerow([]); w.writerow([f"Totals ({len(agg)} suppliers)",
                                        sum(r["c"] for r in agg), tot_g, tot_v, tot_n])
        else:
            w.writerow(["Serial", "Supplier", "Store", "Invoice No", "Invoice Date",
                        "Due Date", "Paid Date", "Amount", "VAT", "Net", "Status"])
            for r in rows:
                w.writerow([r["seq_no"], r["supplier_name"], r["store_name"], r["invoice_number"],
                            r["invoice_date"], r["due_date"], r["paid_date"],
                            r["gross_amount"], r["vat_amount"], r["net_amount"], r["is_paid"]])
            w.writerow([]); w.writerow(["Totals", f"{len(rows)} invoices", "", "", "", "", "",
                                        tot_g, tot_v, tot_n, ""])
        fn = f"report_{report}_{datetime.now().strftime('%Y%m%d')}.csv"
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fn}"})

    _sup = q("SELECT DISTINCT supplier_name s FROM supplier_invoices WHERE supplier_name IS NOT NULL AND supplier_name<>'' ORDER BY supplier_name", (), fetch=True) or []
    supplier_datalist = ("<datalist id='supplierlist'>"
                         + "".join(f"<option value=\"{(r['s'] or '').replace(chr(34), '&quot;')}\">" for r in _sup)
                         + "</datalist>")
    rep_opts = "".join(f"<option value='{v}' {'selected' if v == report else ''}>{l}</option>" for v, l in REPORTS)
    store_opt = lambda s: "selected" if store == s else ""
    _md, month_opts = datetime.now().replace(day=1), ""
    for _ in range(18):
        _m = _md.strftime("%b'%y")
        month_opts += f"<option value=\"{_m}\">{_m}</option>"
        _md = (_md - timedelta(days=1)).replace(day=1)

    shown = rows[:2000]
    store_tot = {}
    for r in rows:
        st = r["store_name"] or ""
        a = store_tot.get(st, [0.0, 0.0, 0.0])
        a[0] += r["gross_amount"] or 0; a[1] += r["vat_amount"] or 0; a[2] += r["net_amount"] or 0
        store_tot[st] = a
    def subtot(st, a):
        return (f"<tr style='background:#eef2f7;font-weight:700'>"
                f"<td colspan='6' style='padding:6px 8px'>{st} sub-total</td>"
                f"<td class='mono' style='text-align:right;padding:6px 8px'>£{a[0]:,.2f}</td>"
                f"<td class='mono' style='text-align:right;padding:6px 8px'>£{a[1]:,.2f}</td>"
                f"<td class='mono' style='text-align:right;padding:6px 8px'>£{a[2]:,.2f}</td></tr>")
    body = ""; cur = None
    for r in shown:
        st = r["store_name"] or ""
        if grouped and cur is not None and st != cur:
            body += subtot(cur, store_tot.get(cur, [0, 0, 0]))
        cur = st
        g = r["gross_amount"] or 0
        gcol = "#dc2626" if g < 0 else "#0f172a"
        body += (f"<tr><td class='mono' style='font-size:12px'>"
                 f"<a href='/invoices?ledger={r['store_name'] or ''}&edit_id={r['invoice_id']}&show_pdf=1' "
                 f"target='_blank' style='color:#2563eb;font-weight:700;text-decoration:none' "
                 f"title='Open this invoice in a new tab'>{r['seq_no'] or ''}</a></td>"
                 f"<td style='font-weight:700'>{r['supplier_name'] or ''}</td>"
                 f"<td style='font-size:12px'>{r['store_name'] or ''}</td>"
                 f"<td class='mono' style='font-size:12px'>{r['invoice_number'] or ''}</td>"
                 f"<td class='mono' style='font-size:12px'>{fmt_uk_date(r['invoice_date'])}</td>"
                 f"<td class='mono' style='font-size:12px'>{fmt_uk_date(r['due_date'])}</td>"
                 f"<td class='mono' style='text-align:right;color:{gcol}'>£{g:,.2f}</td>"
                 f"<td class='mono' style='text-align:right'>£{(r['vat_amount'] or 0):,.2f}</td>"
                 f"<td class='mono' style='text-align:right'>£{(r['net_amount'] or 0):,.2f}</td></tr>")
    if grouped and cur is not None:
        body += subtot(cur, store_tot.get(cur, [0, 0, 0]))
    if not rows:
        body = "<tr><td colspan='9' style='text-align:center;padding:24px;color:#94a3b8'>Set the criteria above and press <b>Run report</b>.</td></tr>"
    cap = (f"<div style='font-size:12px;color:#b45309;padding:4px 0'>Showing first 2,000 of {len(rows):,} rows — the totals cover them all; use Excel export for the full list.</div>"
           if len(rows) > 2000 else "")

    # ── Results table — supplier-summary for "spend", invoice-list otherwise ──
    if is_agg:
        abody = ""
        for r in agg[:2000]:
            g = r["g"] or 0
            gcol = "#dc2626" if g < 0 else "#0f172a"
            abody += (f"<tr><td style='font-weight:700'>{r['supplier_name'] or ''}</td>"
                      f"<td class='mono' style='text-align:right;color:#94a3b8'>{r['c']}</td>"
                      f"<td class='mono' style='text-align:right;color:{gcol}'>£{g:,.2f}</td>"
                      f"<td class='mono' style='text-align:right'>£{(r['v'] or 0):,.2f}</td>"
                      f"<td class='mono' style='text-align:right'>£{(r['n'] or 0):,.2f}</td></tr>")
        if not agg:
            abody = "<tr><td colspan='5' style='text-align:center;padding:24px;color:#94a3b8'>Pick a date range (or leave blank for all-time) and press <b>Run report</b>.</td></tr>"
        results_table = (
            "<div style='overflow-x:auto'><table class='tbl'>"
            "<thead><tr><th>Supplier</th><th style='text-align:right'>Invoices</th>"
            "<th style='text-align:right'>Amount</th><th style='text-align:right'>VAT</th>"
            "<th style='text-align:right'>Net</th></tr></thead>"
            f"<tbody>{abody}</tbody>"
            "<tfoot><tr style='font-weight:800;border-top:2px solid #cbd5e1'>"
            f"<td style='padding:8px'>Totals — {len(agg):,} supplier(s)</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>{sum(r['c'] for r in agg):,}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_g:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_v:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_n:,.2f}</td></tr></tfoot>"
            "</table></div>")
        n_label = f"{len(agg):,} supplier(s)"
    else:
        results_table = (
            "<div style='overflow-x:auto'><table class='tbl'>"
            "<thead><tr><th>Serial</th><th>Supplier</th><th>Store</th><th>Invoice No.</th>"
            "<th>Inv. date</th><th>Due date</th>"
            "<th style='text-align:right'>Amount</th><th style='text-align:right'>VAT</th>"
            "<th style='text-align:right'>Net</th></tr></thead>"
            f"<tbody>{body}</tbody>"
            "<tfoot><tr style='font-weight:800;border-top:2px solid #cbd5e1'>"
            f"<td colspan='6' style='padding:8px'>Totals — {len(rows):,} invoice(s)</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_g:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_v:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_n:,.2f}</td></tr></tfoot>"
            "</table></div>")
        n_label = f"{len(rows):,} invoice(s)"

    from urllib.parse import urlencode
    qs = urlencode({"report": report, "store": store, "supplier": supplier, "date_from": date_from,
                    "date_to": date_to, "due_days": due_days, "exclude_dd": exclude_dd, "sort": sort,
                    "comment": comment, "run": "1"})

    content = f"""
    <style>@media print {{ .noprint {{ display:none !important; }} .card {{ border:none !important; margin:0 !important; }} }}</style>
    <div class='flex justify-between items-center noprint'>
      <div class='text-2xl font-black text-slate-800'>📊 Reports</div>
      <a href='/invoices' class='btn-secondary'>← Back to Invoices</a>
    </div>
    <form method='GET' action='/invoices/reports' class='card noprint' style='margin-top:12px'>
      <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr))'>
        <div><label>Report</label><select name='report' id='rep' onchange='repFields()'>{rep_opts}</select></div>
        <div><label>Store</label><select name='store'>
          <option {store_opt('Both')}>Both</option>
          <option {store_opt('Uxbridge')}>Uxbridge</option>
          <option {store_opt('Newbury')}>Newbury</option></select></div>
        <div data-f='supplier'><label>Supplier</label>
          <input name='supplier' value="{supplier}" list='supplierlist' autocomplete='off' placeholder='(all suppliers)'>{supplier_datalist}</div>
        <div data-f='dates'><label>Date from</label><input type='date' name='date_from' value='{date_from}'></div>
        <div data-f='dates'><label>Date to</label><input type='date' name='date_to' value='{date_to}'></div>
        <div data-f='due'><label>Show due within (days)</label><input type='number' name='due_days' value='{due_days}' min='0' placeholder='0 = overdue only'></div>
        <div><label>Sort by</label><select name='sort'>
          <option value='supplier' {'selected' if sort == 'supplier' else ''}>Supplier</option>
          <option value='invdate' {'selected' if sort == 'invdate' else ''}>Invoice date</option>
          <option value='duedate' {'selected' if sort == 'duedate' else ''}>Due date</option>
          <option value='amount' {'selected' if sort == 'amount' else ''}>Amount</option></select></div>
        <div data-f='comment'><label>Comment contains</label>
          <input name='comment' id='cmt' value="{comment}" placeholder='text in the comment'>
          <select id='cmtmonth' style='margin-top:4px;font-size:12px'><option value=''>— quick fill: filed month —</option>{month_opts}</select></div>
      </div>
      <div class='flex gap-3 mt-3 items-center'>
        <button type='submit' name='run' value='1' class='btn-primary'>▶ Run report</button>
        <label style='display:flex;align-items:center;gap:6px;font-size:13px;color:#475569'>
          <input type='checkbox' name='exclude_dd' value='1' {'checked' if exclude_dd == '1' else ''}> Exclude direct debits</label>
      </div>
    </form>
    <div class='card' style='margin-top:12px'>
      <div class='flex justify-between items-center mb-3'>
        <div class='text-sm font-bold text-slate-600'>{labels[report]} — {n_label}</div>
        <div class='flex gap-2 noprint'>
          <a href='/invoices/reports?{qs}&export=csv' class='btn-secondary' style='font-size:12px'>⬇️ Excel (CSV)</a>
          <button type='button' onclick='window.print()' class='btn-secondary' style='font-size:12px'>🖨️ Print / PDF</button>
        </div>
      </div>
      {cap}
      {results_table}
    </div>
    """ + """
    <script>
    function repFields() {
      var r = document.getElementById('rep').value;
      var m = {supplier:['supplier','dates'], overdue:['due','dates'], upcoming:['supplier','due'],
               period:['dates'], paid:['dates'], unpaid:[], spend:['dates'], comment:['comment']};
      var use = m[r] || [];
      document.querySelectorAll('[data-f]').forEach(function(el){
        var on = use.indexOf(el.getAttribute('data-f')) > -1;
        el.style.opacity = on ? '1' : '0.45';
        el.querySelectorAll('input,select').forEach(function(i){ i.disabled = !on; });
      });
    }
    document.addEventListener('DOMContentLoaded', repFields);
    var mp = document.getElementById('cmtmonth');
    if (mp) mp.addEventListener('change', function(){
      if (mp.value) { document.getElementById('cmt').value = 'Filed at the start of ' + mp.value; }
    });
    </script>
    """
    return page("Reports", content, user, "invoices")


@router.get("/invoices/property-reports", response_class=HTMLResponse)
def property_reports(session: str | None = Cookie(default=None),
                     report: str = "all", prop: str = "All", supplier: str = "",
                     date_from: str = "", date_to: str = "", due_days: str = "14",
                     sort: str = "", run: str = "", export: str = ""):
    """Owner-only property reporting — one combined view across ALL properties
    (104 Dane / 26 Ampth / 53 Ampth / MREL) plus a few reports. View/report only;
    entering invoices is unchanged (still per-property)."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Reports+is+owner-only&msg_type=error", status_code=303)

    REPORTS = [("all", "All properties (list)"), ("overdue", "Overdue / unpaid"),
               ("spend", "Spend per property"), ("supplier", "Spend per supplier")]
    labels = dict(REPORTS)
    if report not in labels:
        report = "all"

    _props = q("SELECT DISTINCT property_name p FROM property_invoices WHERE property_name IS NOT NULL AND property_name<>'' ORDER BY property_name", (), fetch=True) or []
    prop_names = [r["p"] for r in _props]

    conds, params = [], []
    if prop != "All" and prop in prop_names:
        conds.append("property_name=?"); params.append(prop)

    if report == "all":
        if date_from: conds.append("invoice_date>=?"); params.append(date_from)
        if date_to:   conds.append("invoice_date<=?"); params.append(date_to)
    elif report == "overdue":
        conds.append("is_paid!='Yes'")
        conds.append("due_date IS NOT NULL AND due_date<>''")
        if date_to:
            conds.append("due_date<=?"); params.append(date_to)
        else:
            try: n = int(due_days)
            except (TypeError, ValueError): n = 0
            cutoff = (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")
            conds.append("due_date<=?"); params.append(cutoff)
        if date_from:
            conds.append("due_date>=?"); params.append(date_from)
    elif report == "spend":
        if date_from: conds.append("invoice_date>=?"); params.append(date_from)
        if date_to:   conds.append("invoice_date<=?"); params.append(date_to)
    elif report == "supplier":
        if supplier.strip():
            conds.append("supplier_name=?"); params.append(supplier.strip())
        if date_from: conds.append("invoice_date>=?"); params.append(date_from)
        if date_to:   conds.append("invoice_date<=?"); params.append(date_to)

    if not sort:
        sort = {"all": "invdate", "overdue": "duedate"}.get(report, "property")
    sort_col = {"property": "property_name", "supplier": "supplier_name",
                "invdate": "invoice_date", "duedate": "due_date", "amount": "gross_amount"}.get(sort, "property_name")
    grouped = (prop == "All")
    order = f"ORDER BY {'property_name, ' if grouped else ''}{sort_col}, invoice_date"
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    do_run = (run == "1" or export == "csv")
    agg_by = {"spend": "property_name", "supplier": "supplier_name"}.get(report)

    agg = []
    if agg_by:
        agg = (q(f"""SELECT {agg_by} AS k, COUNT(*) AS c,
                            COALESCE(SUM(gross_amount),0) AS g,
                            COALESCE(SUM(vat_amount),0)   AS v,
                            COALESCE(SUM(net_amount),0)   AS n
                     FROM property_invoices {where}
                     GROUP BY {agg_by} ORDER BY SUM(gross_amount) DESC""",
                 tuple(params), fetch=True) or []) if do_run else []
        rows = []
        tot_g = round(sum(r["g"] or 0 for r in agg), 2)
        tot_v = round(sum(r["v"] or 0 for r in agg), 2)
        tot_n = round(sum(r["n"] or 0 for r in agg), 2)
    else:
        rows = (q(f"""SELECT invoice_id, seq_no, supplier_name, property_name, invoice_number, invoice_date,
                             due_date, paid_date, gross_amount, vat_amount, net_amount, is_paid
                      FROM property_invoices {where}
                      {order}""", tuple(params), fetch=True) or []) if do_run else []
        tot_g = round(sum(r["gross_amount"] or 0 for r in rows), 2)
        tot_v = round(sum(r["vat_amount"]   or 0 for r in rows), 2)
        tot_n = round(sum(r["net_amount"]   or 0 for r in rows), 2)

    if export == "csv":
        import csv
        buf = io.StringIO(); w = csv.writer(buf)
        if agg_by:
            head = "Property" if agg_by == "property_name" else "Supplier"
            w.writerow([head, "Invoices", "Amount", "VAT", "Net"])
            for r in agg:
                w.writerow([r["k"], r["c"], r["g"], r["v"], r["n"]])
            w.writerow([]); w.writerow([f"Totals ({len(agg)})", sum(r["c"] for r in agg), tot_g, tot_v, tot_n])
        else:
            w.writerow(["Serial", "Supplier", "Property", "Invoice No", "Invoice Date",
                        "Due Date", "Paid Date", "Amount", "VAT", "Net", "Status"])
            for r in rows:
                w.writerow([r["seq_no"], r["supplier_name"], r["property_name"], r["invoice_number"],
                            r["invoice_date"], r["due_date"], r["paid_date"],
                            r["gross_amount"], r["vat_amount"], r["net_amount"], r["is_paid"]])
            w.writerow([]); w.writerow(["Totals", f"{len(rows)} invoices", "", "", "", "", "",
                                        tot_g, tot_v, tot_n, ""])
        fn = f"property_report_{report}_{datetime.now().strftime('%Y%m%d')}.csv"
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fn}"})

    from urllib.parse import urlencode, quote as _q
    _sup = q("SELECT DISTINCT supplier_name s FROM property_invoices WHERE supplier_name IS NOT NULL AND supplier_name<>'' ORDER BY supplier_name", (), fetch=True) or []
    supplier_datalist = ("<datalist id='psupplierlist'>"
                         + "".join(f"<option value=\"{(r['s'] or '').replace(chr(34), '&quot;')}\">" for r in _sup)
                         + "</datalist>")
    rep_opts = "".join(f"<option value='{v}' {'selected' if v == report else ''}>{l}</option>" for v, l in REPORTS)
    prop_opts = (f"<option value='All' {'selected' if prop == 'All' else ''}>All properties</option>"
                 + "".join(f"<option value=\"{html.escape(str(p))}\" {'selected' if prop == p else ''}>{html.escape(str(p))}</option>" for p in prop_names))

    shown = rows[:2000]
    prop_tot = {}
    for r in rows:
        k = r["property_name"] or ""
        a = prop_tot.get(k, [0.0, 0.0, 0.0])
        a[0] += r["gross_amount"] or 0; a[1] += r["vat_amount"] or 0; a[2] += r["net_amount"] or 0
        prop_tot[k] = a
    def subtot(k, a):
        return (f"<tr style='background:#eef2f7;font-weight:700'>"
                f"<td colspan='6' style='padding:6px 8px'>{k} sub-total</td>"
                f"<td class='mono' style='text-align:right;padding:6px 8px'>£{a[0]:,.2f}</td>"
                f"<td class='mono' style='text-align:right;padding:6px 8px'>£{a[1]:,.2f}</td>"
                f"<td class='mono' style='text-align:right;padding:6px 8px'>£{a[2]:,.2f}</td></tr>")
    body = ""; cur = None
    for r in shown:
        k = r["property_name"] or ""
        if grouped and cur is not None and k != cur:
            body += subtot(cur, prop_tot.get(cur, [0, 0, 0]))
        cur = k
        g = r["gross_amount"] or 0
        gcol = "#dc2626" if g < 0 else "#0f172a"
        led = _q("PROP:" + (r["property_name"] or ""))
        body += (f"<tr><td class='mono' style='font-size:12px'>"
                 f"<a href='/invoices?ledger={led}&edit_id={r['invoice_id']}&show_pdf=1' target='_blank' "
                 f"style='color:#2563eb;font-weight:700;text-decoration:none' title='Open this invoice in a new tab'>{r['seq_no'] or ''}</a></td>"
                 f"<td style='font-weight:700'>{r['supplier_name'] or ''}</td>"
                 f"<td style='font-size:12px'>{r['property_name'] or ''}</td>"
                 f"<td class='mono' style='font-size:12px'>{r['invoice_number'] or ''}</td>"
                 f"<td class='mono' style='font-size:12px'>{fmt_uk_date(r['invoice_date'])}</td>"
                 f"<td class='mono' style='font-size:12px'>{fmt_uk_date(r['due_date'])}</td>"
                 f"<td class='mono' style='text-align:right;color:{gcol}'>£{g:,.2f}</td>"
                 f"<td class='mono' style='text-align:right'>£{(r['vat_amount'] or 0):,.2f}</td>"
                 f"<td class='mono' style='text-align:right'>£{(r['net_amount'] or 0):,.2f}</td></tr>")
    if grouped and cur is not None:
        body += subtot(cur, prop_tot.get(cur, [0, 0, 0]))
    if not rows and not agg_by:
        body = "<tr><td colspan='9' style='text-align:center;padding:24px;color:#94a3b8'>Choose your criteria and press <b>Run report</b>.</td></tr>"
    cap = (f"<div style='font-size:12px;color:#b45309;padding:4px 0'>Showing first 2,000 of {len(rows):,} rows — totals cover them all; use Excel export for the full list.</div>"
           if len(rows) > 2000 else "")

    if agg_by:
        head = "Property" if agg_by == "property_name" else "Supplier"
        abody = ""
        for r in agg[:2000]:
            g = r["g"] or 0
            gcol = "#dc2626" if g < 0 else "#0f172a"
            abody += (f"<tr><td style='font-weight:700'>{r['k'] or ''}</td>"
                      f"<td class='mono' style='text-align:right;color:#94a3b8'>{r['c']}</td>"
                      f"<td class='mono' style='text-align:right;color:{gcol}'>£{g:,.2f}</td>"
                      f"<td class='mono' style='text-align:right'>£{(r['v'] or 0):,.2f}</td>"
                      f"<td class='mono' style='text-align:right'>£{(r['n'] or 0):,.2f}</td></tr>")
        if not agg:
            abody = "<tr><td colspan='5' style='text-align:center;padding:24px;color:#94a3b8'>Choose your criteria and press <b>Run report</b>.</td></tr>"
        results_table = (
            "<div style='overflow-x:auto'><table class='tbl'>"
            f"<thead><tr><th>{head}</th><th style='text-align:right'>Invoices</th>"
            "<th style='text-align:right'>Amount</th><th style='text-align:right'>VAT</th>"
            "<th style='text-align:right'>Net</th></tr></thead>"
            f"<tbody>{abody}</tbody>"
            "<tfoot><tr style='font-weight:800;border-top:2px solid #cbd5e1'>"
            f"<td style='padding:8px'>Totals — {len(agg)} {head.lower()}(s)</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>{sum(r['c'] for r in agg):,}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_g:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_v:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_n:,.2f}</td></tr></tfoot>"
            "</table></div>")
        n_label = f"{len(agg)} {head.lower()}(s)"
    else:
        results_table = (
            "<div style='overflow-x:auto'><table class='tbl'>"
            "<thead><tr><th>Serial</th><th>Supplier</th><th>Property</th><th>Invoice No.</th>"
            "<th>Inv. date</th><th>Due date</th>"
            "<th style='text-align:right'>Amount</th><th style='text-align:right'>VAT</th>"
            "<th style='text-align:right'>Net</th></tr></thead>"
            f"<tbody>{body}</tbody>"
            "<tfoot><tr style='font-weight:800;border-top:2px solid #cbd5e1'>"
            f"<td colspan='6' style='padding:8px'>Totals — {len(rows):,} invoice(s)</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_g:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_v:,.2f}</td>"
            f"<td class='mono' style='text-align:right;padding:8px'>£{tot_n:,.2f}</td></tr></tfoot>"
            "</table></div>")
        n_label = f"{len(rows):,} invoice(s)"

    qs = urlencode({"report": report, "prop": prop, "supplier": supplier, "date_from": date_from,
                    "date_to": date_to, "due_days": due_days, "sort": sort, "run": "1"})

    content = f"""
    <style>@media print {{ .noprint {{ display:none !important; }} .card {{ border:none !important; margin:0 !important; }} }}</style>
    <div class='flex justify-between items-center noprint'>
      <div class='text-2xl font-black text-slate-800'>🏠 Property Reports</div>
      <a href='/invoices' class='btn-secondary'>← Back to Invoices</a>
    </div>
    <form method='GET' action='/invoices/property-reports' class='card noprint' style='margin-top:12px'>
      <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr))'>
        <div><label>Report</label><select name='report' id='rep' onchange='repFields()'>{rep_opts}</select></div>
        <div><label>Property</label><select name='prop'>{prop_opts}</select></div>
        <div data-f='supplier'><label>Supplier</label>
          <input name='supplier' value="{html.escape(supplier)}" list='psupplierlist' autocomplete='off' placeholder='(all suppliers)'>{supplier_datalist}</div>
        <div data-f='dates'><label>Date from</label><input type='date' name='date_from' value='{date_from}'></div>
        <div data-f='dates'><label>Date to</label><input type='date' name='date_to' value='{date_to}'></div>
        <div data-f='due'><label>Show due within (days)</label><input type='number' name='due_days' value='{due_days}' min='0' placeholder='0 = overdue only'></div>
        <div><label>Sort by</label><select name='sort'>
          <option value='property' {'selected' if sort == 'property' else ''}>Property</option>
          <option value='supplier' {'selected' if sort == 'supplier' else ''}>Supplier</option>
          <option value='invdate' {'selected' if sort == 'invdate' else ''}>Invoice date</option>
          <option value='duedate' {'selected' if sort == 'duedate' else ''}>Due date</option>
          <option value='amount' {'selected' if sort == 'amount' else ''}>Amount</option></select></div>
      </div>
      <div class='flex gap-3 mt-3 items-center'>
        <button type='submit' name='run' value='1' class='btn-primary'>▶ Run report</button>
      </div>
    </form>
    <div class='card' style='margin-top:12px'>
      <div class='flex justify-between items-center mb-3'>
        <div class='text-sm font-bold text-slate-600'>{labels[report]} — {n_label}</div>
        <div class='flex gap-2 noprint'>
          <a href='/invoices/property-reports?{qs}&export=csv' class='btn-secondary' style='font-size:12px'>⬇️ Excel (CSV)</a>
          <button type='button' onclick='window.print()' class='btn-secondary' style='font-size:12px'>🖨️ Print / PDF</button>
        </div>
      </div>
      {cap}
      {results_table}
    </div>
    """ + """
    <script>
    function repFields() {
      var r = document.getElementById('rep').value;
      var m = {all:['supplier','dates'], overdue:['due','dates'], spend:['dates'], supplier:['dates']};
      var use = m[r] || [];
      document.querySelectorAll('[data-f]').forEach(function(el){
        var on = use.indexOf(el.getAttribute('data-f')) > -1;
        el.style.opacity = on ? '1' : '0.45';
        el.querySelectorAll('input,select').forEach(function(i){ i.disabled = !on; });
      });
    }
    document.addEventListener('DOMContentLoaded', repFields);
    </script>
    """
    return page("Property Reports", content, user, "invoices")


@router.get("/invoices/supplier-terms", response_class=HTMLResponse)
def supplier_terms(session: str | None = Cookie(default=None), msg: str = "", msg_type: str = "success"):
    """Owner-only screen to set each supplier's payment terms, which auto-fill
    the due date on invoices. Lists every supplier you've invoiced (most-used
    first); ones with no rule fall back to manual due-date entry."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Supplier+terms+is+owner-only&msg_type=error", status_code=303)

    rows = q("""
        SELECT s.supplier_name sn, s.n cnt, t.term_type tt, t.term_value tv, t.pays_dd dd, t.vat_reclaim_pct vp
        FROM (SELECT supplier_name, COUNT(*) n FROM (
                SELECT supplier_name FROM supplier_invoices
                UNION ALL SELECT supplier_name FROM property_invoices) GROUP BY supplier_name) s
        LEFT JOIN supplier_terms t ON t.supplier_name = s.supplier_name
        ORDER BY s.n DESC, s.supplier_name
    """, (), fetch=True) or []

    def opts(sel):
        out = ""
        for v, lbl in [("", "— Manual (enter due date by hand) —"),
                       ("days", "Net … days"), ("eom", "End of month + … months")]:
            out += f"<option value='{v}' {'selected' if (sel or '')==v else ''}>{lbl}</option>"
        return out

    tr = ""
    for i, r in enumerate(rows):
        needs = (r["tt"] is None)
        esc = str(r['sn']).replace('"', '&quot;')
        tr += (f"<tr class='strow' style=\"{'background:#fffbeb' if needs else ''}\">"
               f"<input type='hidden' name='sup_{i}' value=\"{esc}\">"
               f"<td style='font-weight:700'>{r['sn']}</td>"
               f"<td class='mono' style='text-align:right;color:#94a3b8;font-size:12px'>{r['cnt']}</td>"
               f"<td><select name='type_{i}' style='padding:4px 6px'>{opts(r['tt'])}</select></td>"
               f"<td><input type='number' name='val_{i}' value='{r['tv'] if r['tv'] is not None else ''}' "
               f"min='0' style='width:90px;padding:4px 6px' placeholder='days / months'></td>"
               f"<td style='text-align:center'><input type='checkbox' name='dd_{i}' value='Yes' "
               f"{'checked' if r['dd']=='Yes' else ''}></td>"
               f"<td style='text-align:center'><input type='number' name='vatpct_{i}' value='{r['vp'] if r['vp'] is not None else ''}' "
               f"min='0' max='100' style='width:70px;padding:4px 6px' placeholder='100' "
               f"title='% of VAT you can reclaim; blank/100 = full. e.g. 50 for company-car leases'></td>"
               f"<td><button type='button' class='renamebtn' data-sup=\"{esc}\" "
               f"style='font-size:12px;padding:3px 8px;border:1px solid #cbd5e1;border-radius:6px;"
               f"background:white;cursor:pointer'>✏️ Rename</button></td></tr>")

    flash = ""
    if msg:
        colour = "#16a34a" if msg_type == "success" else "#dc2626"
        bg     = "#f0fdf4" if msg_type == "success" else "#fef2f2"
        flash = (f"<div style='background:{bg};border:1px solid {colour};color:{colour};"
                 f"border-radius:10px;padding:12px 16px;margin-bottom:12px;font-weight:700'>{msg}</div>")

    content = f"""
    {flash}
    <div class='flex justify-between items-center'>
      <div class='text-2xl font-black text-slate-800'>📅 Supplier payment terms</div>
      <a href='/invoices' class='btn-secondary'>← Back to Invoices</a>
    </div>
    <div class='card' style='margin-top:12px;font-size:13px;color:#475569'>
      Set how each supplier's <b>due date</b> is worked out. <b>Net days</b> = invoice date + N days.
      <b>End of month + months</b> = the last day of the month N months after the invoice
      (e.g. EOM+1 on a March invoice → 30 April). Amber rows have no rule yet and fall back to
      manual entry. The due date on an invoice can always be overridden.
    </div>
    <div class='card' style='margin-top:12px'>
      <input type='text' id='filt' placeholder='🔍 Filter suppliers…'
        style='width:100%;max-width:320px;padding:6px 10px;margin-bottom:10px'
        onkeyup="document.getElementById('filt').dataset.txt=this.value.toLowerCase();stApply();">
      <div id='azbar' style='display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px'>
        <button type='button' onclick="stLetter('')" style='padding:3px 9px;border:1px solid #cbd5e1;border-radius:6px;background:#0f2942;color:white;cursor:pointer;font-weight:700'>All</button>
        {"".join(f"<button type='button' onclick=\"stLetter('{ch}')\" style='padding:3px 9px;border:1px solid #cbd5e1;border-radius:6px;background:white;cursor:pointer;font-weight:700'>{ch}</button>" for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
      </div>
      <form method='POST' action='/invoices/supplier-terms/save'>
        <div style='overflow-x:auto;max-height:60vh;overflow-y:auto'>
          <table class='tbl'>
            <thead><tr>
              <th style='cursor:pointer' onclick='sortSt(0,false)'>Supplier ⇅</th>
              <th style='cursor:pointer;text-align:right' onclick='sortSt(1,true)'>Invoices ⇅</th>
              <th style='cursor:pointer' onclick='sortSt(2,false)'>Term type ⇅</th>
              <th>Days / Months</th><th title='Auto-set Payment Method to Direct Debit on new invoices'>Pays by DD?</th><th title='% of VAT reclaimable; blank = 100%. e.g. 50 for company-car leases'>VAT reclaim %</th><th>Tidy up</th></tr></thead>
            <tbody id='sttbody'>{tr or "<tr><td colspan='5' style='text-align:center;padding:24px;color:#94a3b8'>No suppliers yet</td></tr>"}</tbody>
          </table>
        </div>
        <div style='margin-top:12px'><button type='submit' class='btn-primary'>💾 Save terms</button></div>
      </form>
    </div>
    <script>
      var stLet='';
      function stLetter(ch) {{
        stLet=ch;
        document.querySelectorAll('#azbar button').forEach(function(b){{
          const on=(ch===''&&b.innerText==='All')||b.innerText===ch;
          b.style.background=on?'#0f2942':'white'; b.style.color=on?'white':'';
        }});
        stApply();
      }}
      function stApply() {{
        const txt=(document.getElementById('filt').dataset.txt||'');
        document.querySelectorAll('.strow').forEach(function(r){{
          const name=(r.querySelectorAll('td')[0].innerText||'').trim();
          const okL = stLet==='' || name.toUpperCase().charAt(0)===stLet;
          const okT = txt==='' || r.innerText.toLowerCase().indexOf(txt)>-1;
          r.style.display=(okL&&okT)?'':'none';
        }});
      }}
      function sortSt(col, numeric) {{
        const tb=document.getElementById('sttbody');
        const rows=Array.from(tb.querySelectorAll('tr.strow'));
        const asc = tb.dataset.col===String(col) ? tb.dataset.asc!=='1' : true;
        rows.sort(function(a,b){{
          let x=a.querySelectorAll('td')[col], y=b.querySelectorAll('td')[col];
          x = col===2 ? (x.querySelector('select')||{{}}).value||'' : x.innerText.trim();
          y = col===2 ? (y.querySelector('select')||{{}}).value||'' : y.innerText.trim();
          if(numeric){{ return asc ? (parseFloat(x)||0)-(parseFloat(y)||0) : (parseFloat(y)||0)-(parseFloat(x)||0); }}
          return asc ? String(x).localeCompare(y) : String(y).localeCompare(x);
        }});
        rows.forEach(function(r){{ tb.appendChild(r); }});
        tb.dataset.col=col; tb.dataset.asc=asc?'1':'0';
      }}
      document.querySelectorAll('.renamebtn').forEach(function(b){{
        b.addEventListener('click', function(){{
          const oldName=b.dataset.sup;
          const nn=prompt('Rename supplier — this relabels ALL its invoices.\\n'+
                          'If the new name matches another supplier, they merge.\\n\\nSupplier:', oldName);
          if(nn===null) return;
          const v=nn.trim();
          if(!v || v===oldName) return;
          const f=document.createElement('form');
          f.method='POST'; f.action='/invoices/supplier-rename';
          const i1=document.createElement('input'); i1.name='old_name'; i1.value=oldName;
          const i2=document.createElement('input'); i2.name='new_name'; i2.value=v;
          f.appendChild(i1); f.appendChild(i2); document.body.appendChild(f); f.submit();
        }});
      }});
    </script>"""
    return page("Supplier payment terms", content, user, "invoices")


@router.post("/invoices/supplier-terms/save")
async def supplier_terms_save(request: Request, session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Owner+only&msg_type=error", status_code=303)

    form = await request.form()
    conn = db(); cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uname = user.get("username", "")
    i, saved = 0, 0
    while True:
        sup = form.get(f"sup_{i}")
        if sup is None:
            break
        ttype = (form.get(f"type_{i}") or "").strip()
        raw = form.get(f"val_{i}")
        try:
            tval = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            tval = None
        # Forgiving: a number entered without choosing a type means "Net N days".
        if tval is not None and ttype not in ("days", "eom"):
            ttype = "days"
        has_term = ttype in ("days", "eom") and tval is not None
        if not has_term:
            ttype, tval = None, None
        dd_flag = "Yes" if form.get(f"dd_{i}") else None
        # VAT reclaim %: blank or >=100 = fully reclaimable (stored NULL = 100%);
        # a value like 50 = only 50% reclaimable (e.g. company-car leases).
        vraw = form.get(f"vatpct_{i}")
        try:
            vpct = int(vraw) if vraw not in (None, "") else None
        except (TypeError, ValueError):
            vpct = None
        if vpct is not None and (vpct >= 100 or vpct < 0):
            vpct = None
        if has_term or dd_flag == "Yes" or vpct is not None:
            cur.execute("""INSERT INTO supplier_terms
                             (supplier_name, term_type, term_value, pays_dd, vat_reclaim_pct, updated_by, updated_at)
                           VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT(supplier_name) DO UPDATE SET
                             term_type=excluded.term_type, term_value=excluded.term_value,
                             pays_dd=excluded.pays_dd, vat_reclaim_pct=excluded.vat_reclaim_pct,
                             updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                        (sup, ttype, tval, dd_flag, vpct, uname, now))
            if has_term:
                saved += 1
        else:
            cur.execute("DELETE FROM supplier_terms WHERE supplier_name=?", (sup,))
        i += 1
    conn.commit(); conn.close()
    from urllib.parse import quote as urlquote
    return RedirectResponse(f"/invoices/supplier-terms?msg={urlquote(f'Saved — {saved} supplier rule(s) set.')}&msg_type=success",
                            status_code=303)


@router.post("/invoices/supplier-rename")
async def supplier_rename(request: Request, session: str | None = Cookie(default=None)):
    """Owner-only: rename a supplier across all its invoices (both ledgers). If
    the new name already exists, the two merge. No data is lost — it relabels."""
    redir, user = require_login(session)
    if redir: return redir
    if user.get("role") != "owner":
        return RedirectResponse("/invoices?msg=Owner+only&msg_type=error", status_code=303)

    form = await request.form()
    old = (form.get("old_name") or "").strip()
    new = (form.get("new_name") or "").strip()
    from urllib.parse import quote as urlquote
    if not old or not new or old == new:
        return RedirectResponse("/invoices/supplier-terms", status_code=303)

    n1 = q("SELECT COUNT(*) c FROM supplier_invoices WHERE supplier_name=?", (old,), fetch=True)[0]["c"]
    n2 = q("SELECT COUNT(*) c FROM property_invoices WHERE supplier_name=?", (old,), fetch=True)[0]["c"]
    q("UPDATE supplier_invoices SET supplier_name=? WHERE supplier_name=?", (new, old))
    q("UPDATE property_invoices SET supplier_name=? WHERE supplier_name=?", (new, old))

    # Move the term rule to the new name only if the new name has none yet;
    # otherwise keep the new name's existing rule and drop the old one.
    oldrule = q("SELECT 1 FROM supplier_terms WHERE supplier_name=?", (old,), fetch=True)
    newrule = q("SELECT 1 FROM supplier_terms WHERE supplier_name=?", (new,), fetch=True)
    if oldrule and not newrule:
        q("UPDATE supplier_terms SET supplier_name=? WHERE supplier_name=?", (new, old))
    else:
        q("DELETE FROM supplier_terms WHERE supplier_name=?", (old,))

    msg = f"Renamed “{old}” → “{new}” ({n1 + n2} invoice(s) updated)."
    return RedirectResponse(f"/invoices/supplier-terms?msg={urlquote(msg)}&msg_type=success",
                            status_code=303)
