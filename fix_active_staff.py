import sqlite3

conn = sqlite3.connect("business_vault.db")
cur  = conn.cursor()

# The definitive current staff list from Report (Current Employees)
current_staff = [
    ("Jessica",      "Amati"),
    ("Jade",         "Kingham"),
    ("Jasmine",      "Tidbury"),
    ("Daniel",       "Gamlin"),
    ("Chariie",      "Kirby"),
    ("Gian",         "Mhina"),
    ("Katie",        "Crocker"),
    ("Kaleem",       "Ahmad"),
    ("Rhys Michael", "Sears"),
    ("David",        "Place"),
    ("Hafsa",        "Furqan"),
    ("Kalli",        "Willson"),
]

# Mark ALL staff as inactive first
cur.execute("UPDATE staff_profiles SET is_active=0")

# Then mark only the current ones as active
for first, last in current_staff:
    cur.execute("""UPDATE staff_profiles SET is_active=1
                   WHERE first_name=? AND last_name=?""",
                (first, last))
    if cur.rowcount == 0:
        print(f"  ⚠️  Not found: {first} {last}")
    else:
        print(f"  ✅ Active: {first} {last}")

conn.commit()

# Summary
cur.execute("SELECT COUNT(*) FROM staff_profiles WHERE is_active=1")
active = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM staff_profiles WHERE is_active=0")
leavers = cur.fetchone()[0]
print(f"\n✅ Done — {active} active staff, {leavers} leavers")
conn.close()
