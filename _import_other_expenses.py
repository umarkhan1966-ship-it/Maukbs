"""One-off import of the property workbook's 'Other Expenses' sheet into
property_invoices, under property_name='MREL' and approval_status='pending' so
the owner can review/correct each. Serials continue the property sequence.
Take a DB backup first. Safe to delete after use.

NOTE: re-running the main property Data import (_import_property.py) DELETEs all
property_invoices, which would remove these too. Run this AFTER the Data import,
and don't re-run the Data import without re-running this.
"""
import sqlite3, datetime
import openpyxl

XLSX = "Invoice record (BTL)_Form).xlsm"
DB   = "business_vault.db"

def d(v):
    if v is None or v == "":
        return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]

def num(v):
    try:    return float(v)
    except (TypeError, ValueError): return None

def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True, read_only=True)
    ws = wb["Other Expenses"]

    conn = sqlite3.connect(DB, timeout=30)
    cur  = conn.cursor()
    next_seq = (cur.execute("SELECT COALESCE(MAX(seq_no),0) FROM property_invoices").fetchone()[0]) + 1
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    records = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None and row[1] is None and row[2] is None:
            continue
        records.append((
            next_seq,                 # seq_no (continue property sequence)
            "MREL",                   # property_name
            (str(row[1]).strip() if row[1] else None),  # supplier_name
            d(row[0]),                # invoice_date
            num(row[2]),              # gross_amount
            num(row[3]),              # vat_amount
            num(row[4]),              # net_amount
            (str(row[5]).strip() if row[5] else None),   # comments
            "No",                     # is_paid (unknown — confirm on review)
            "pending",                # approval_status — for owner review
            "import",                 # submitted_by
            now,                      # created_at
        ))
        next_seq += 1

    cur.executemany("""
        INSERT INTO property_invoices
        (seq_no, property_name, supplier_name, invoice_date, gross_amount,
         vat_amount, net_amount, comments, is_paid, approval_status,
         submitted_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, records)
    conn.commit()
    print(f"Imported {len(records)} Other Expenses rows as pending MREL.")
    print("Property total now:", cur.execute("SELECT COUNT(*) FROM property_invoices").fetchone()[0])
    print("Pending MREL count:", cur.execute(
        "SELECT COUNT(*) FROM property_invoices WHERE property_name='MREL' AND approval_status='pending'").fetchone()[0])
    print("Serial range now:", cur.execute("SELECT MIN(seq_no), MAX(seq_no) FROM property_invoices").fetchone()[0:2])
    conn.close()

if __name__ == "__main__":
    main()
