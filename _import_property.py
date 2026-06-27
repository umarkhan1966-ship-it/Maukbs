"""One-off import of the property (BTL) invoice Excel 'Data' sheet into
property_invoices. Replaces existing property_invoices rows. Take a DB backup
first. Safe to delete after use."""
import sqlite3, datetime
import openpyxl

XLSX = "Invoice record (BTL)_Form).xlsm"
DB   = "business_vault.db"

# Excel "Property" (full address / MREL) -> app short_name used as property_name.
PROP_MAP = {
    "104 dane road":   "104 Dane",
    "53 ampthill way": "53 Ampth",
    "26 ampthill way": "26 Ampth",
    "mrel":            "MREL",
}

def d(v):
    if v is None or v == "":
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]

def num(v):
    try:    return float(v)
    except (TypeError, ValueError): return None

def i(v):
    try:    return int(round(float(v)))
    except (TypeError, ValueError): return None

def method(row):
    # cols: 20 DD, 21 Crd, 22 O/L, 23 Csh, 24 Chq
    for idx, name in [(20, "Direct Debit"), (22, "Online"), (21, "Card"),
                      (24, "Cheque"), (23, "Cash")]:
        if row[idx]:
            return name
    return None

def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True, read_only=True)
    ws = wb["Data"]

    records, unmapped = [], {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None and row[1] is None:
            continue
        raw_prop = (str(row[2]).strip() if row[2] else "")
        prop = PROP_MAP.get(raw_prop.lower())
        if not prop:
            unmapped[raw_prop] = unmapped.get(raw_prop, 0) + 1
            continue

        gross   = num(row[9])
        paid_dt = d(row[16])
        amt_paid = num(row[17])
        is_paid = "Yes" if paid_dt else "No"
        if is_paid == "Yes" and (amt_paid is None or amt_paid == 0):
            amt_paid = gross

        records.append((
            i(row[0]),            # seq_no  (S.No)
            prop,                 # property_name
            (str(row[1]).strip() if row[1] else None),  # supplier_name
            (str(row[5]).strip() if row[5] else None),  # invoice_number
            d(row[8]),            # invoice_date
            None,                 # expense_type (not in sheet)
            gross,                # gross_amount
            num(row[10]),         # vat_amount
            num(row[11]),         # net_amount
            d(row[13]),           # due_date
            paid_dt,              # paid_date
            amt_paid,             # amount_paid
            0,                    # credit_note
            is_paid,              # is_paid
            method(row),          # payment_method
            (str(row[25]).strip() if row[25] else None),  # cheque_number
            d(row[3]),            # accountant_sent_date (Dt. Submtd)
            (str(row[26]).strip() if row[26] else None),  # comments
            "approved",           # approval_status
            "import",             # submitted_by
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # created_at
        ))

    conn = sqlite3.connect(DB, timeout=30)
    cur  = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(property_invoices)")}
    for name, ddl in [("seq_no", "seq_no INTEGER"), ("cheque_number", "cheque_number TEXT"),
                      ("accountant_sent_date", "accountant_sent_date TEXT")]:
        if name not in cols:
            cur.execute(f"ALTER TABLE property_invoices ADD COLUMN {ddl}")

    before = cur.execute("SELECT COUNT(*) FROM property_invoices").fetchone()[0]
    cur.execute("DELETE FROM property_invoices")
    cur.executemany("""
        INSERT INTO property_invoices
        (seq_no, property_name, supplier_name, invoice_number, invoice_date,
         expense_type, gross_amount, vat_amount, net_amount, due_date,
         paid_date, amount_paid, credit_note, is_paid, payment_method,
         cheque_number, accountant_sent_date, comments, approval_status,
         submitted_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, records)
    conn.commit()
    after = cur.execute("SELECT COUNT(*) FROM property_invoices").fetchone()[0]
    print(f"Rows before: {before}")
    print(f"Rows imported: {len(records)}")
    print(f"Rows now: {after}")
    print(f"Unmapped properties (skipped): {unmapped}")
    print("Per property:", dict(cur.execute(
        "SELECT property_name, COUNT(*) FROM property_invoices GROUP BY property_name")))
    conn.close()

if __name__ == "__main__":
    main()
