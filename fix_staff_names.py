import sqlite3
conn = sqlite3.connect("business_vault.db")
cur  = conn.cursor()

# Fix typo: Chariie → Charlie
cur.execute("UPDATE staff_profiles SET first_name='Charlie' WHERE first_name='Chariie'")
print(f"Chariie → Charlie: {cur.rowcount} updated")

# Fix Rhys Michael → add alias so import can find him
# The sheet is 'Rhys_Leave' so we need first_name to start with 'Rhys'
# Change 'Rhys Michael' to 'Rhys' and put 'Michael' in a middle name or keep in last
cur.execute("UPDATE staff_profiles SET first_name='Rhys', last_name='Michael Sears' WHERE first_name='Rhys Michael' AND last_name='Sears'")
print(f"Rhys Michael Sears → Rhys Michael Sears: {cur.rowcount} updated")

conn.commit()

# Verify
print("\nUpdated records:")
for r in conn.execute("SELECT staff_id, first_name, last_name FROM staff_profiles WHERE is_active=1 ORDER BY first_name").fetchall():
    print(f"  {r[0]}: {r[1]} {r[2]}")

conn.close()
print("\n✅ Done")
