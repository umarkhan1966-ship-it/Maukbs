import sqlite3
conn = sqlite3.connect("business_vault.db")
cur  = conn.cursor()

# ── 1. Show all current active staff with IDs ──
print("Current active staff:")
all_active = conn.execute(
    "SELECT staff_id, first_name, last_name, store_name, date_joined FROM staff_profiles WHERE is_active=1 ORDER BY first_name, staff_id"
).fetchall()
for r in all_active:
    print(f"  ID:{r[0]} {r[1]} {r[2]} — {r[3]} — joined:{r[4]}")

# ── 2. Mark ALL as inactive first ──
cur.execute("UPDATE staff_profiles SET is_active=0")

# ── 3. Fix name typos ──
cur.execute("UPDATE staff_profiles SET first_name='Charlie' WHERE first_name='Chariie'")
cur.execute("UPDATE staff_profiles SET first_name='Rhys' WHERE first_name='Rhys Michael'")

# ── 4. Set correct 12 active staff by name ──
current_staff = [
    ("Jessica","Amati"), ("Jade","Kingham"), ("Jasmine","Tidbury"),
    ("Daniel","Gamlin"), ("Charlie","Kirby"), ("Gian","Mhina"),
    ("Katie","Crocker"), ("Kaleem","Ahmad"), ("Rhys","Sears"),
    ("David","Place"), ("Hafsa","Furqan"), ("Kalli","Willson"),
]
for first, last in current_staff:
    # Only activate the ONE with the highest staff_id (most recent record)
    row = conn.execute(
        "SELECT MAX(staff_id) FROM staff_profiles WHERE first_name=? AND last_name=?",
        (first, last)
    ).fetchone()
    if row and row[0]:
        cur.execute("UPDATE staff_profiles SET is_active=1 WHERE staff_id=?", (row[0],))
        print(f"  Activated: ID:{row[0]} {first} {last}")
    else:
        print(f"  ⚠️ Not found: {first} {last}")

# ── 5. Remove duplicate property invoices ──
cur.execute("""
    DELETE FROM property_invoices
    WHERE invoice_id NOT IN (
        SELECT MIN(invoice_id)
        FROM property_invoices
        GROUP BY property_name, supplier_name,
                 COALESCE(invoice_number,''), COALESCE(invoice_date,'')
    )
""")
print(f"\nRemoved {cur.rowcount} duplicate property invoices")

# ── 6. Clear imported leave for fresh import ──
cur.execute("DELETE FROM leave_requests WHERE requested_by='import'")
print(f"Cleared leave records for fresh import")

conn.commit()

# ── Summary ──
print()
active = conn.execute("SELECT COUNT(*) FROM staff_profiles WHERE is_active=1").fetchone()[0]
leavers = conn.execute("SELECT COUNT(*) FROM staff_profiles WHERE is_active=0").fetchone()[0]
props = conn.execute("SELECT COUNT(*) FROM property_invoices").fetchone()[0]
print(f"  Active staff:      {active}  (should be 12)")
print(f"  Leavers:           {leavers}")
print(f"  Property invoices: {props}  (should be 83)")
print()
print("Active staff:")
for r in conn.execute("SELECT staff_id, first_name, last_name, store_name FROM staff_profiles WHERE is_active=1 ORDER BY first_name").fetchall():
    print(f"  ID:{r[0]} {r[1]} {r[2]} — {r[3]}")

conn.close()
print("\n✅ Done — now run: python import_historical_data.py")
