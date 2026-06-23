import sqlite3

conn = sqlite3.connect("business_vault.db")
cur  = conn.cursor()

# Find and delete duplicate staff — keep the first (lowest id) for each unique first+last+date_joined
cur.execute("""
    DELETE FROM staff_profiles
    WHERE staff_id NOT IN (
        SELECT MIN(staff_id)
        FROM staff_profiles
        GROUP BY first_name, last_name, COALESCE(date_joined, "")
    )
""")
deleted = cur.rowcount
conn.commit()

cur.execute("SELECT COUNT(*) FROM staff_profiles")
remaining = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM staff_profiles WHERE is_active=1")
active = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM staff_profiles WHERE is_active=0")
leavers = cur.fetchone()[0]

print(f"✅ Removed {deleted} duplicate staff records")
print(f"   Remaining: {remaining} total ({active} active, {leavers} leavers)")
conn.close()
