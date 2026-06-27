"""One-off import of the supplier invoice Excel into supplier_invoices.
Replaces all existing supplier_invoices rows. A DB backup is taken separately
before running this. Safe to delete after use."""
import sqlite3, datetime, sys
import openpyxl

XLSX = "Supplier's Invoice record (Uxbr-Newb)_Form.xlsm"
DB   = "business_vault.db"

STORE_MAP = {"uxbr": "Uxbridge", "newb": "Newbury"}

def d(v):
    """Excel date -> 'YYYY-MM-DD' or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def i(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None

def method(row):
    # cols: 22 DD, 23 Card, 24 Amex, 25 Online, 26 Cash, 27 Cheque
    for idx, name in [(22, "Direct Debit"), (25, "Online"), (23, "Card"),
                      (24, "Amex"), (27, "Cheque"), (26, "Cash")]:
        if row[idx]:
            return name
    return None

def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True, read_only=True)
    ws = wb["Data"]

    records = []
    skipped = 0
    bad_store = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None and row[1] is None:
            continue
        raw_store = (str(row[2]).strip().lower() if row[2] else "")
        store = STORE_MAP.get(raw_store)
        if not store:
            bad_store[row[2]] = bad_store.get(row[2], 0) + 1
            skipped += 1
            continue

        gross = num(row[11])
        paid_dt = d(row[18])
        amt_paid = num(row[19])
        is_paid = "Yes" if paid_dt else "No"
        # A paid invoice with no recorded paid amount is assumed paid in full,
        # so the app's balance shows 0 rather than the full amount outstanding.
        if is_paid == "Yes" and (amt_paid is None or amt_paid == 0):
            amt_paid = gross

        records.append((
            store,                # store_name
            i(row[0]),            # seq_no  (S.No)
            (str(row[1]).strip() if row[1] else None),  # supplier_name
            (str(row[7]).strip() if row[7] else None),  # invoice_number
            d(row[10]),           # invoice_date
            gross,                # gross_amount
            num(row[12]),         # vat_amount
            num(row[13]),         # net_amount
            i(row[14]),           # payment_terms
            d(row[15]),           # due_date
            paid_dt,              # paid_date
            amt_paid,             # amount_paid
            0,                    # credit_note
            is_paid,              # is_paid
            method(row),          # payment_method
            (str(row[29]).strip() if row[29] else None),  # comments
            d(row[16]),           # dd_statement_date (DD Stmnt Dt)
            (str(row[28]).strip() if row[28] else None),  # cheque_number (Chq No)
            "approved",           # approval_status
            "import",             # submitted_by
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # created_at
        ))

    conn = sqlite3.connect(DB, timeout=30)
    cur = conn.cursor()
    before = cur.execute("SELECT COUNT(*) FROM supplier_invoices").fetchone()[0]

    # Rebuild the table WITHOUT the over-strict UNIQUE(invoice_number, store_name)
    # rule, which conflicts with genuine same-number invoices from different
    # suppliers. Duplicate protection stays at the application level (the save
    # warning). Columns/types/defaults are preserved; the two new fields are
    # included inline.
    cur.executescript("""
        PRAGMA foreign_keys = OFF;
        DROP TABLE IF EXISTS supplier_invoices_old;
        ALTER TABLE supplier_invoices RENAME TO supplier_invoices_old;
        CREATE TABLE supplier_invoices (
            invoice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            seq_no         INTEGER,
            supplier_name  TEXT NOT NULL,
            store_name     TEXT NOT NULL,
            invoice_number TEXT,
            invoice_date   TEXT,
            gross_amount   REAL DEFAULT 0,
            vat_amount     REAL DEFAULT 0,
            net_amount     REAL DEFAULT 0,
            payment_terms  INTEGER,
            due_date       TEXT,
            paid_date      TEXT,
            amount_paid    REAL DEFAULT 0,
            credit_note    REAL DEFAULT 0,
            is_paid        TEXT DEFAULT 'No',
            payment_method TEXT,
            comments       TEXT,
            pdf_path          TEXT,
            approval_status   TEXT DEFAULT 'approved',
            submitted_by      TEXT,
            created_at        TEXT DEFAULT (date('now')),
            dd_statement_date TEXT,
            cheque_number     TEXT
        );
        DROP TABLE supplier_invoices_old;
    """)
    cur.executemany("""
        INSERT INTO supplier_invoices
        (store_name, seq_no, supplier_name, invoice_number, invoice_date,
         gross_amount, vat_amount, net_amount, payment_terms, due_date,
         paid_date, amount_paid, credit_note, is_paid, payment_method,
         comments, dd_statement_date, cheque_number, approval_status,
         submitted_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, records)
    conn.commit()
    after = cur.execute("SELECT COUNT(*) FROM supplier_invoices").fetchone()[0]

    print(f"Rows before wipe : {before}")
    print(f"Rows imported    : {len(records)}")
    print(f"Rows now in table: {after}")
    print(f"Skipped (bad store): {skipped}  detail={bad_store}")
    conn.close()

if __name__ == "__main__":
    main()
