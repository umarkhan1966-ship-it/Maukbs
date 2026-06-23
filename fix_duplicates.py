import sqlite3
conn = sqlite3.connect("business_vault.db")
cur  = conn.cursor()

# Keep highest ID (most recent), mark older duplicates as leavers
cur.execute("UPDATE staff_profiles SET is_active=0 WHERE staff_id IN (15, 22)")
conn.commit()

rows = conn.execute("SELECT staff_id, first_name, last_name, store_name FROM staff_profiles WHERE is_active=1 ORDER BY first_name").fetchall()
print(f"Active staff ({len(rows)}):")
for r in rows:
    print(f"  {r[1]} {r[2]} — {r[3]}")
conn.close()
