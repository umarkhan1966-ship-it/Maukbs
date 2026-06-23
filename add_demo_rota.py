"""
add_demo_rota.py — with break deduction
30 min break deducted for shifts of 4 hours or more.
"""
import sqlite3
from datetime import datetime, timedelta

DB_FILE = "business_vault.db"

def calc_paid_hours(raw_hrs):
    if not raw_hrs: return 0.0
    return round(raw_hrs - 0.5, 2) if raw_hrs >= 4.0 else round(raw_hrs, 2)

def parse_hours(start, end):
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return (eh*60+em - sh*60-sm) / 60

conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
cur  = conn.cursor()

today = datetime.now()
days_since_sunday = (today.weekday() + 1) % 7
week_start = (today - timedelta(days=days_since_sunday)).strftime("%Y-%m-%d")
week_dates = [(datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

print(f"Week: {week_start}")

# Clear existing shifts for this week
cur.execute("DELETE FROM rota_shifts WHERE shift_date BETWEEN ? AND ?", (week_dates[0], week_dates[-1]))
cur.execute("DELETE FROM rotas WHERE week_start=?", (week_start,))
conn.commit()

patterns = [
    [None, "09:00-17:00", "09:00-17:00", None,         "09:00-17:00", "09:00-17:00", "09:00-14:00"],
    [None, None,          "10:00-16:00", "10:00-16:00",  None,         "10:00-16:00", "10:00-17:00"],
    ["10:00-15:00", None, None,          "10:00-17:00", "10:00-17:00", None,          "09:00-17:00"],
    [None, "12:00-17:00", None,          "12:00-17:00",  None,         "12:00-17:00", "10:00-14:00"],
    [None, "09:00-13:00", "09:00-13:00", None,          "09:00-13:00", "09:00-13:00", None],
]

for store in ["Newbury", "Uxbridge"]:
    staff = conn.execute(
        "SELECT * FROM staff_profiles WHERE store_name=? AND is_active=1 ORDER BY first_name",
        (store,)
    ).fetchall()
    if not staff:
        print(f"  No staff for {store}")
        continue

    cur.execute("INSERT INTO rotas (store_name,week_start,status) VALUES(?,?,'draft')", (store, week_start))
    conn.commit()
    rota_id = conn.execute("SELECT rota_id FROM rotas WHERE store_name=? AND week_start=?",
                           (store, week_start)).fetchone()["rota_id"]

    print(f"\n{store} (rota_id={rota_id}):")
    for idx, s in enumerate(staff):
        sid     = s["staff_id"]
        name    = f"{s['first_name']} {s['last_name']}"
        pattern = patterns[idx % len(patterns)]

        for day_idx, date_str in enumerate(week_dates):
            shift_str = pattern[day_idx]
            if shift_str is None:
                cur.execute("INSERT INTO rota_shifts (rota_id,staff_id,shift_date,is_off) VALUES(?,?,?,1)",
                            (rota_id, sid, date_str))
            else:
                start, end = shift_str.split("-")
                raw  = parse_hours(start, end)
                paid = calc_paid_hours(raw)
                cur.execute("""INSERT INTO rota_shifts
                    (rota_id,staff_id,shift_date,shift_start,shift_end,hours,is_off)
                    VALUES(?,?,?,?,?,?,0)""",
                    (rota_id, sid, date_str, start, end, paid))

        # Show one example
        ex = pattern[1] or pattern[2] or ""
        if ex:
            raw  = parse_hours(*ex.split("-"))
            paid = calc_paid_hours(raw)
            print(f"  ✅ {name} — e.g. {ex} = {raw:.1f}h raw → {paid:.1f}h paid")
        else:
            print(f"  ✅ {name}")

        # Save template
        for dow, shift_str in enumerate(pattern):
            if shift_str is None:
                cur.execute("""INSERT OR REPLACE INTO rota_templates
                    (staff_id,store_name,day_of_week,is_off) VALUES(?,?,?,1)""",
                    (sid, store, dow))
            else:
                start, end = shift_str.split("-")
                paid = calc_paid_hours(parse_hours(start, end))
                cur.execute("""INSERT OR REPLACE INTO rota_templates
                    (staff_id,store_name,day_of_week,shift_start,shift_end,hours,is_off)
                    VALUES(?,?,?,?,?,?,0)""",
                    (sid, store, dow, start, end, paid))

conn.commit()
conn.close()
print(f"\n✅ Done — refresh browser and click 'Today' on the Rota page")
