import sqlite3
conn = sqlite3.connect("business_vault.db")
rows = conn.execute("SELECT staff_id, first_name, last_name, store_name FROM staff_profiles WHERE is_active=1 ORDER BY first_name").fetchall()
print(f"Active staff ({len(rows)}):")
for r in rows:
    print(f"  ID:{r[0]} {r[1]} {r[2]} — {r[3]}")
conn.close()
