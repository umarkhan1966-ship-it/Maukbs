import sqlite3
conn = sqlite3.connect("business_vault.db")

# Check leave_requests table
rows = conn.execute("SELECT COUNT(*) FROM leave_requests").fetchone()
print(f"Total leave requests in database: {rows[0]}")

rows = conn.execute("SELECT * FROM leave_requests LIMIT 10").fetchall()
if rows:
    for r in rows:
        print(r)
else:
    print("No leave records found")

# Check if Full Record files are readable
import os
for f in ["Full Record (358).xls", "Full Record (372).xls"]:
    exists = os.path.exists(f)
    print(f"File '{f}': {'EXISTS' if exists else 'NOT FOUND'}")

conn.close()
