"""
create_test_user.py
Creates a test staff login to try the portal on your phone.
Run: python create_test_user.py
"""
import sqlite3
import hashlib

conn = sqlite3.connect("business_vault.db")
cur  = conn.cursor()

# Create a test staff account
users = [
    # (username, password, full_name, role, store)
    ("jessica.amati",  "snappy123", "Jessica Amati",  "staff",   "Newbury"),
    ("jade.kingham",   "snappy123", "Jade Kingham",   "staff",   "Newbury"),
    ("kaleem.ahmad",   "snappy123", "Kaleem Ahmad",   "staff",   "Uxbridge"),
    ("rhys.sears",     "snappy123", "Rhys Sears",     "staff",   "Uxbridge"),
    ("newbury.mgr",    "snappy123", "Newbury Manager","manager", "Newbury"),
    ("uxbridge.mgr",   "snappy123", "Uxbridge Manager","manager","Uxbridge"),
]

for username, password, full_name, role, store in users:
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    try:
        cur.execute("""INSERT OR IGNORE INTO users
            (username, password, full_name, role, store_name, is_active)
            VALUES(?,?,?,?,?,1)""",
            (username, pw_hash, full_name, role, store))
        print(f"✅ Created: {username} / {password} ({role} — {store})")
    except Exception as e:
        print(f"⚠️  {username}: {e}")

conn.commit()
conn.close()
print("\n✅ Done — try logging in with any of the above")
print("Default password for all: snappy123")
