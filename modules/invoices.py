"""invoices routes."""
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


def extract_pdf_data(pdf_bytes: bytes) -> dict:
    """Try to extract invoice fields from a PDF using pdfplumber.
    Returns a dict with whatever fields we can find — caller fills the rest manually."""
    result = {}
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        lines = text.split("\n")
        full  = text.lower()

        # Supplier name — a line with a company suffix is the strongest signal,
        # otherwise fall back to the first meaningful (non-label) line.
        for line in lines[:10]:
            s = line.strip()
            if any(w in s.lower() for w in
                   ["ltd", "limited", "plc", "llp", "& co", "group", "services"]):
                result["supplier_name"] = s
                break
        if not result.get("supplier_name"):
            for line in lines[:6]:
                s = line.strip()
                if len(s) > 3 and not any(w in s.lower() for w in
                   ["invoice", "tax", "vat", "date", "statement", "remittance"]):
                    result["supplier_name"] = s
                    break
        if not result.get("supplier_name") and lines:
            result["supplier_name"] = lines[0].strip()

        # Invoice number — look for "invoice no", "inv no", "invoice #", "invoice number"
        inv_patterns = [
            r"invoice\s*(?:no\.?|number|#)[:\s]+([A-Z0-9\-\/]+)",
            r"inv\.?\s*(?:no\.?|#)[:\s]+([A-Z0-9\-\/]+)",
            r"(?:^|\s)(INV[-\s]?[0-9]+)",
        ]
        for pat in inv_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                result["invoice_number"] = m.group(1).strip()
                break

        # Invoice date
        date_patterns = [
            r"(?:invoice\s*date|date\s*of\s*invoice|date)[:\s]+([0-9]{1,2}[\s/\-][A-Za-z0-9]{1,3}[\s/\-][0-9]{2,4})",
            r"(?:dated?)[:\s]+([0-9]{1,2}[\s/\-][A-Za-z0-9]{2,3}[\s/\-][0-9]{2,4})",
            r"([0-9]{2}/[0-9]{2}/[0-9]{4})",
            r"([0-9]{2}-[0-9]{2}-[0-9]{4})",
            r"([0-9]{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+[0-9]{4})",
        ]
        for pat in date_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                # Try to parse and normalise to YYYY-MM-DD
                from datetime import datetime as dt
                for fmt in ("%d/%m/%Y","%d-%m-%Y","%d %B %Y","%d %b %Y",
                            "%d/%m/%y","%B %d, %Y","%b %d, %Y"):
                    try:
                        result["invoice_date"] = dt.strptime(raw, fmt).strftime("%Y-%m-%d")
                        break
                    except: pass
                if not result.get("invoice_date"):
                    result["invoice_date_raw"] = raw
                break

        # Gross / total amount — try the most specific "final total" labels first,
        # and use a lookbehind so plain "total" never matches inside "subtotal".
        amount_patterns = [
            r"(?:grand\s*total|total\s*due|balance\s*due|amount\s*due|total\s*payable)[:\s£]+([0-9,]+\.[0-9]{2})",
            r"(?:total\s*inc\.?\s*vat|total\s*including\s*vat)[:\s£]+([0-9,]+\.[0-9]{2})",
            r"(?<![a-z])total[:\s£]+([0-9,]+\.[0-9]{2})",
            r"£\s*([0-9,]+\.[0-9]{2})\s*$",
        ]
        for pat in amount_patterns:
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                try:
                    result["gross_amount"] = float(m.group(1).replace(",",""))
                    break
                except: pass

        # VAT amount
        vat_patterns = [
            r"(?:vat|tax)[:\s£]+([0-9,]+\.?[0-9]*)",
            r"(?:vat\s*@\s*20%)[:\s£]+([0-9,]+\.?[0-9]*)",
        ]
        for pat in vat_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    result["vat_amount"] = float(m.group(1).replace(",",""))
                    break
                except: pass

        # Net amount
        net_patterns = [
            r"(?:net|subtotal|sub\s*total|amount\s*ex\.?\s*vat)[:\s£]+([0-9,]+\.?[0-9]*)",
        ]
        for pat in net_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    result["net_amount"] = float(m.group(1).replace(",",""))
                    break
                except: pass

        # If we have gross and vat but no net, calculate it
        if result.get("gross_amount") and result.get("vat_amount") and not result.get("net_amount"):
            result["net_amount"] = round(result["gross_amount"] - result["vat_amount"], 2)

        # Payment terms
        terms_m = re.search(r"(?:payment\s*terms?|net)[:\s]+(\d+)\s*days?", text, re.IGNORECASE)
        if terms_m:
            result["payment_terms"] = int(terms_m.group(1))

        result["_raw_text"] = text[:500]  # first 500 chars for debugging

    except Exception as e:
        result["_error"] = str(e)

    return result


UPLOAD_DIR = "invoice_pdfs"


PAYMENT_METHODS = ["", "Direct Debit", "Card", "Cash", "Cheque", "Online", "Amex"]


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
                   pg: int, page_size: int = 30):
    is_prop = is_property_ledger(ledger)
    table   = "property_invoices" if is_prop else "supplier_invoices"
    loc_col = "property_name"     if is_prop else "store_name"
    loc_val = prop_name(ledger)   if is_prop else ledger

    conds  = [f"{loc_col} = ?"]
    params = [loc_val]

    if search.strip():
        conds.append("(supplier_name LIKE ? OR invoice_number LIKE ? OR CAST(seq_no AS TEXT) LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]

    today = datetime.now().strftime("%Y-%m-%d")
    if status == "overdue":
        conds.append(f"is_paid != 'Yes' AND due_date < '{today}' AND COALESCE(approval_status,'approved')='approved'")
    elif status == "unpaid":
        conds.append("is_paid != 'Yes' AND COALESCE(approval_status,'approved')='approved'")
    elif status == "paid":
        conds.append("is_paid = 'Yes'")
    elif status == "partial":
        conds.append("is_paid != 'Yes' AND amount_paid > 0")
    elif status == "pending":
        conds.append("approval_status = 'pending'")
    else:
        # Default: exclude pending from main view unless owner/manager reviewing
        pass

    where  = "WHERE " + " AND ".join(conds)
    total  = q(f"SELECT COUNT(*) as n FROM {table} {where}", params, fetch=True)
    total_n = total[0]["n"] if total else 0

    balance_expr = "COALESCE(gross_amount,0)-COALESCE(amount_paid,0)-COALESCE(credit_note,0)"
    rows = q(f"""
        SELECT *, {balance_expr} AS balance
        FROM {table} {where}
        ORDER BY due_date ASC, invoice_id DESC
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

    invoices, total_n = fetch_invoices(ledger, search, status, pg, PAGE_SIZE)
    total_pages = max(1, (total_n + PAGE_SIZE - 1) // PAGE_SIZE)

    # Pending approvals count (managers/owners only)
    pending_count = 0
    if user["role"] in ("owner", "manager"):
        p1 = q(f"SELECT COUNT(*) as n FROM {table} WHERE {loc_col}=? AND approval_status='pending'",
               (loc_val,), fetch=True)
        pending_count = p1[0]["n"] if p1 else 0

    # Summary totals for this ledger
    tots = q(f"""
        SELECT
          COUNT(*) as total_count,
          COALESCE(SUM(CASE WHEN is_paid!='Yes' AND due_date < '{today}' THEN gross_amount-amount_paid-credit_note ELSE 0 END),0) as overdue_val,
          COUNT(CASE WHEN is_paid!='Yes' AND due_date < '{today}' THEN 1 END) as overdue_count,
          COALESCE(SUM(CASE WHEN is_paid='Yes' THEN amount_paid ELSE 0 END),0) as paid_val
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
        <div class='text-xs font-bold text-slate-400 uppercase'>Total Paid (YTD)</div>
        <div class='text-2xl font-black text-emerald-600'>£{t.get('paid_val',0):,.2f}</div>
      </div>
    </div>"""

    # ── Search & filter bar ──
    status_opts = ""
    for val, label in [("","All"),("overdue","Overdue"),("unpaid","Unpaid"),
                        ("partial","Partial"),("paid","Paid")]:
        sel = "selected" if val == status else ""
        status_opts += f"<option value='{val}' {sel}>{label}</option>"

    search_bar = f"""
    <div class='card'>
      <form method='GET' action='/invoices' class='flex flex-wrap gap-3 items-end'>
        <input type='hidden' name='ledger' value='{ledger}'>
        <div style='flex:2;min-width:200px'>
          <label>Search supplier, invoice no. or serial no.</label>
          <input type='text' name='search' value='{search}'
            placeholder='e.g. Bestway, INV-001, 42...'>
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

    def fi(name, label, ftype="text", val=None, req=False, opts=None, placeholder=""):
        """Render a form field."""
        safe_val = val if val is not None else ""
        req_attr = "required" if req else ""
        step     = "step='0.01'" if ftype == "number" else ""
        ph       = f"placeholder='{placeholder}'" if placeholder else ""
        if opts is not None:
            o_html = ""
            for ov, ol in opts:
                sel = "selected" if str(safe_val) == str(ov) else ""
                o_html += f"<option value='{ov}' {sel}>{ol}</option>"
            return f"<div><label>{label}</label><select name='{name}' {req_attr}>{o_html}</select></div>"
        return f"<div><label>{label}</label><input type='{ftype}' name='{name}' value='{safe_val}' {req_attr} {step} {ph}></div>"

    # Payment status fields (only show if editing)
    payment_fields = ""
    if is_edit:
        paid_opts  = [("No","Unpaid"),("Yes","Paid")]
        meth_opts  = [(m, m or "-- Select --") for m in PAYMENT_METHODS]
        balance    = (inv.get("gross_amount") or 0) - (inv.get("amount_paid") or 0) - (inv.get("credit_note") or 0)
        payment_fields = f"""
        <div class='col-span-2' style='border-top:1px solid #e2e8f0;padding-top:12px;margin-top:4px'>
          <div class='text-xs font-bold text-slate-500 uppercase tracking-wide mb-3'>Payment Details</div>
          <div class='grid gap-3' style='grid-template-columns:repeat(auto-fit,minmax(150px,1fr))'>
            {fi('is_paid',        'Status',          opts=paid_opts,  val=inv.get('is_paid','No'))}
            {fi('paid_date',      'Paid Date',        'date',          inv.get('paid_date',''))}
            {fi('payment_method', 'Payment Method',   opts=meth_opts,  val=inv.get('payment_method',''))}
            {fi('amount_paid',    'Amount Paid (£)',  'number',        inv.get('amount_paid',0))}
            {fi('credit_note',    'Credit Note (£)',  'number',        inv.get('credit_note',0))}
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
    
    # Seq no (retail only) — auto-fill the next serial in line for new invoices
    if is_prop:
        seq_field = ""
    else:
        seq_default = inv.get('seq_no', '')
        if not is_edit:
            mx  = q("SELECT MAX(seq_no) AS m FROM supplier_invoices WHERE store_name=?",
                    (loc_val,), fetch=True)
            seq_default = ((dict(mx[0]).get('m') or 0) + 1) if mx else 1
        seq_field = fi('seq_no', 'Serial No.', 'number', seq_default)

    # Thumbnail preview of the attached PDF (edit mode, when a file is attached)
    pdf_preview = ""
    if is_edit and inv.get("pdf_path"):
        thumb_url = f"/invoices/pdf-thumb/{edit_id}?ledger={ledger}"
        full_url  = f"/invoices/pdf/{edit_id}?ledger={ledger}"
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
          </div>
        </div>"""

    form_html = f"""
    <div class='card' id='invoice-form'>
      <!-- PDF Upload — one file does both: auto-fills fields AND saves with invoice -->
      <div style='background:#f0f9ff;border:1px solid #bae6fd;border-radius:10px;padding:12px 16px;margin-bottom:16px'>
        <div style='font-size:13px;font-weight:700;color:#0369a1;margin-bottom:8px'>
          📎 Attach Invoice PDF
          <span style='font-weight:400;color:#64748b;font-size:12px;margin-left:8px'>
            — uploads once, auto-fills fields AND saves the PDF with the record
          </span>
        </div>
        <div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap'>
          <input type='file' name='pdf_file' id='pdf_prefill' accept='.pdf'
            form='invoiceForm' onchange='extractPdf()'
            style='flex:1;min-width:200px;border:1px solid #bae6fd;background:white;padding:5px 10px;border-radius:8px;font-size:13px'>
          <span id='pdf_status' style='font-size:12px;color:#0369a1'></span>
        </div>
        <div style='font-size:11px;color:#94a3b8;margin-top:6px'>
          Fields auto-fill from the PDF where possible. Check and adjust anything that looks wrong before saving.
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
          {seq_field}
          {fi('supplier_name',  'Supplier Name',    val=inv.get('supplier_name',''),  req=True)}
          {fi('invoice_number', 'Invoice Number',   val=inv.get('invoice_number',''))}
          {fi('invoice_date',   'Invoice Date',     'date', inv.get('invoice_date',''))}
          {fi('due_date',       'Due Date',         'date', inv.get('due_date',''))}
          {fi('gross_amount',   'Gross Amount (£)', 'number', inv.get('gross_amount',0))}
          {fi('vat_amount',     'VAT Amount (£)',   'number', inv.get('vat_amount',0))}
          {fi('net_amount',     'Net Amount (£)',   'number', inv.get('net_amount',0))}
          {fi('payment_terms',  'Terms (days)',     'number', inv.get('payment_terms',''))}
          {prop_or_store_field}
          <!-- PDF attached via the strip above -->
          <div style='grid-column:1/-1'>
            {fi('comments','Comments', val=inv.get('comments',''))}
          </div>
          {payment_fields}
        </div>
        <div class='flex gap-3 mt-4'>
          <button type='submit' class='btn-primary'>{'💾 Update Invoice' if is_edit else '➕ Save Invoice'}</button>
          {'<a href="/invoices/delete/' + str(edit_id) + '?ledger=' + ledger + '" class="btn-danger" onclick=\"return confirm(\'Delete this invoice?\');\">🗑️ Delete</a>' if is_edit else ''}
          <a href='{cancel_url}' class='btn-secondary'>Cancel</a>
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
            badge = "<span class='badge-overdue'>OVERDUE</span>"
            row_cls = "style='background:#fff5f5'"
        elif paid > 0:
            badge = "<span class='badge-partial'>PARTIAL</span>"
            row_cls = "style='background:#fffbeb'"
        else:
            badge = "<span class='badge-unpaid'>UNPAID</span>"
            row_cls = ""

        seq_td = f"<td class='mono' style='color:#94a3b8;font-size:11px'>{row['seq_no'] or ''}</td>" if not is_prop else ""
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
          <td class='mono' style='font-size:12px;color:#64748b'>{row['invoice_date'] or '—'}</td>
          <td class='mono' style='font-size:12px;color:#64748b'>{row['due_date'] or '—'}</td>
          <td class='mono' style='font-weight:700'>£{row['gross_amount']:,.2f}</td>
          <td class='mono' style='color:#16a34a'>{'£'+f'{paid:,.2f}' if paid else '—'}</td>
          <td class='mono' style='font-weight:700;color:{"#dc2626" if balance > 0 else "#16a34a"}'>£{balance:,.2f}</td>
          <td>{badge}</td>
          <td style='font-size:12px;color:#64748b'>{row['payment_method'] or '—'}</td>
          <td>{pdf_td}</td>
          <td>{approval_td}</td>
        </tr>"""

    # Seq header only for retail
    seq_th = "<th>Serial</th>" if not is_prop else ""

    # Pagination
    pag_html = ""
    if total_pages > 1:
        base = f"/invoices?ledger={ledger}&search={search}&status={status}&page="
        pag_html = "<div class='flex gap-2 flex-wrap justify-center'>"
        for p in range(1, total_pages + 1):
            cls = "btn-primary" if p == pg else "btn-secondary"
            pag_html += f"<a href='{base}{p}' class='{cls}' style='padding:6px 14px'>{p}</a>"
        pag_html += "</div>"

    list_html = f"""
    <div class='card' style='padding:0;overflow:hidden'>
      <div style='padding:16px 20px;background:#0f2942;display:flex;justify-content:space-between;align-items:center'>
        <div style='color:white;font-weight:700;font-size:14px'>
          {total_n} invoices
          {'· <span style="color:#fbbf24">'+str(t.get('overdue_count',0))+' overdue</span>' if t.get('overdue_count',0) > 0 else ''}
        </div>
        <div style='color:#93c5fd;font-size:12px'>Click any row to edit</div>
      </div>
      <div style='overflow-x:auto'>
        <table class='tbl'>
          <thead>
            <tr>
              {seq_th}
              <th>Supplier</th><th>Invoice No.</th><th>Inv. Date</th>
              <th>Due Date</th><th>Gross</th><th>Paid</th>
              <th>Balance</th><th>Status</th><th>Method</th><th>PDF</th>
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
    });

    // ── PDF auto-fill ──
    async function extractPdf() {
      const fileInput = document.getElementById('pdf_prefill');
      const status    = document.getElementById('pdf_status');
      if (!fileInput.files.length) return;
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
            el.style.background = '#f0fdf4';  // green tint = auto-filled
          }
        };
        fill('supplier_name',  data.supplier_name);
        fill('invoice_number', data.invoice_number);
        fill('invoice_date',   data.invoice_date);
        fill('gross_amount',   data.gross_amount);
        fill('vat_amount',     data.vat_amount);
        fill('net_amount',     data.net_amount);
        fill('payment_terms',  data.payment_terms);

        // Trigger calculations for any fields NOT found in PDF
        const gross = document.querySelector('[name="gross_amount"]');
        if (gross) gross.dispatchEvent(new Event('input'));
        const idate = document.querySelector('[name="invoice_date"]');
        if (idate) idate.dispatchEvent(new Event('change'));

        let found = Object.keys(data).filter(k => !k.startsWith('_') && data[k]).length;
        status.textContent = '✅ ' + found + ' fields found — please check and adjust as needed';
        status.style.color = '#16a34a';

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
      <a href='/invoices/recent-payments' class='btn-secondary' style='margin-left:auto'>📋 Recent Payments</a>
    </div>"""

    content = "\n".join([flash, ledger_switcher, summary, search_bar, form_html, list_html, js])
    return page("Invoices", content, user, "invoices")


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
        try: return float(form.get(key, 0) or 0)
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
    inv_no     = fv("invoice_number")
    inv_date   = fv("invoice_date") or None
    due_date   = fv("due_date")     or None
    gross      = fnum("gross_amount")
    vat        = fnum("vat_amount")
    net        = fnum("net_amount")
    terms      = fint("payment_terms")
    comments   = fv("comments")     or None
    is_paid    = fv("is_paid", "No")
    paid_date  = fv("paid_date")    or None
    pay_method = fv("payment_method") or None
    amt_paid   = fnum("amount_paid")
    credit     = fnum("credit_note")
    seq_no     = fint("seq_no")
    exp_type   = fv("expense_type") or None

    if not supplier:
        return RedirectResponse(f"/invoices?ledger={ledger}&msg=Supplier+name+is+required&msg_type=error",
                                status_code=303)

    from urllib.parse import quote as urlquote

    # ── Approval status based on role ──
    role = user.get("role", "staff")
    approval_status = "approved" if role in ("owner", "manager") else "pending"
    submitted_by    = user.get("username", "")

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
      <input type='hidden' name='supplier_name'   value='{supplier}'>
      <input type='hidden' name='invoice_number'  value='{inv_no}'>
      <input type='hidden' name='invoice_date'    value='{fv("invoice_date")}'>
      <input type='hidden' name='due_date'        value='{fv("due_date")}'>
      <input type='hidden' name='gross_amount'    value='{gross}'>
      <input type='hidden' name='vat_amount'      value='{vat}'>
      <input type='hidden' name='net_amount'      value='{net}'>
      <input type='hidden' name='payment_terms'   value='{terms or ""}'>
      <input type='hidden' name='comments'        value='{fv("comments")}'>
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

    # ── Validation warnings (non-blocking, stored as comment note) ──
    warnings = []
    if gross > 0 and vat > 0:
        expected_vat = round(gross / 6, 2)
        if abs(vat - expected_vat) > 1.0:
            warnings.append(f"VAT £{vat:.2f} doesn't match standard 20% (expected ~£{expected_vat:.2f})")
    if gross > 10000:
        warnings.append(f"Large invoice amount: £{gross:,.2f} — please double-check")
    if due_date and due_date < datetime.now().strftime("%Y-%m-%d"):
        warnings.append("Due date is in the past")
    warning_note = (" | WARNINGS: " + "; ".join(warnings)) if warnings else ""
    if warning_note and comments:
        comments = comments + warning_note
    elif warning_note:
        comments = warning_note.strip(" | ")

    if invoice_id == 0:
        # New invoice
        if is_prop:
            q(f"""INSERT OR IGNORE INTO {table}
                (property_name, supplier_name, invoice_number, invoice_date,
                 expense_type, gross_amount, vat_amount, net_amount, due_date,
                 payment_terms, comments, is_paid, pdf_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (loc_val, supplier, inv_no, inv_date, exp_type,
               gross, vat, net, due_date, terms, comments, is_paid, pdf_path))
        else:
            q(f"""INSERT OR IGNORE INTO {table}
                (store_name, seq_no, supplier_name, invoice_number, invoice_date,
                 gross_amount, vat_amount, net_amount, due_date, payment_terms,
                 comments, is_paid, pdf_path, approval_status, submitted_by)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (loc_val, seq_no, supplier, inv_no, inv_date,
               gross, vat, net, due_date, terms, comments, is_paid, pdf_path,
               approval_status, submitted_by))
        if approval_status == "pending":
            msg = f"Invoice submitted for approval — {supplier} {inv_no}"
        else:
            msg = f"Invoice added — {supplier} {inv_no}"
    else:
        # Update existing
        if is_prop:
            q(f"""UPDATE {table} SET
                supplier_name=?, invoice_number=?, invoice_date=?,
                expense_type=?, gross_amount=?, vat_amount=?, net_amount=?,
                due_date=?, payment_terms=?, comments=?, is_paid=?,
                paid_date=?, payment_method=?, amount_paid=?,
                {', pdf_path=?' if pdf_path else ''}
                WHERE invoice_id=?""",
              ([supplier, inv_no, inv_date, exp_type, gross, vat, net,
                due_date, terms, comments, is_paid, paid_date, pay_method, credit]
               + ([pdf_path] if pdf_path else []) + [invoice_id]))
        else:
            q(f"""UPDATE {table} SET
                seq_no=?, supplier_name=?, invoice_number=?, invoice_date=?,
                gross_amount=?, vat_amount=?, net_amount=?,
                due_date=?, payment_terms=?, comments=?, is_paid=?,
                paid_date=?, payment_method=?, amount_paid=?, credit_note=?
                {', pdf_path=?' if pdf_path else ''}
                WHERE invoice_id=?""",
              ([seq_no, supplier, inv_no, inv_date, gross, vat, net,
                due_date, terms, comments, is_paid, paid_date,
                pay_method, amt_paid, credit]
               + ([pdf_path] if pdf_path else []) + [invoice_id]))
        msg = f"Invoice updated — {supplier} {inv_no}"

    from urllib.parse import quote as urlquote
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
    if user["role"] not in ("owner", "manager"):
        return RedirectResponse(f"/invoices?ledger={ledger}&msg=Not+authorised&msg_type=error",
                                status_code=303)
    table = "property_invoices" if is_property_ledger(ledger) else "supplier_invoices"
    q(f"DELETE FROM {table} WHERE invoice_id=?", (invoice_id,))
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
def recent_payments(session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir

    from collections import defaultdict

    rows = q("""
        SELECT 'retail' as ledger_type, store_name as location,
               supplier_name, invoice_number, gross_amount,
               amount_paid, credit_note, paid_date, payment_method, is_paid,
               COALESCE(gross_amount,0)-COALESCE(amount_paid,0)-COALESCE(credit_note,0) as balance
        FROM supplier_invoices
        WHERE paid_date IS NOT NULL OR amount_paid > 0
        UNION ALL
        SELECT 'property', property_name,
               supplier_name, invoice_number, gross_amount,
               amount_paid, 0, paid_date, payment_method, is_paid,
               COALESCE(gross_amount,0)-COALESCE(amount_paid,0) as balance
        FROM property_invoices
        WHERE paid_date IS NOT NULL OR amount_paid > 0
        ORDER BY paid_date DESC
        LIMIT 200
    """, fetch=True) or []

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
            📅 {date_key}
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
