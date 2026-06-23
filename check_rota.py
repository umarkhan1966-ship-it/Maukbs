import sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect("business_vault.db")
conn.row_factory = sqlite3.Row

# Check what's in rotas table
rotas = conn.execute("SELECT * FROM rotas ORDER BY week_start DESC LIMIT 5").fetchall()
print("=== Rotas ===")
for r in rotas:
    print(f"  ID:{r['rota_id']} {r['store_name']} week:{r['week_start']} status:{r['status']}")

# Check shifts
shifts = conn.execute("SELECT COUNT(*) as n FROM rota_shifts").fetchone()
print(f"\nTotal rota_shifts: {shifts['n']}")

# Show sample shifts
sample = conn.execute("""
    SELECT rs.*, sp.first_name, sp.last_name
    FROM rota_shifts rs
    JOIN staff_profiles sp ON rs.staff_id=sp.staff_id
    LIMIT 10
""").fetchall()
print("\nSample shifts:")
for s in sample:
    print(f"  {s['first_name']} {s['last_name']} | {s['shift_date']} | {'OFF' if s['is_off'] else s['shift_start']+'-'+str(s['shift_end'])} | {s['hours']}h")

# Check current week
today = datetime.now()
days_since_sunday = (today.weekday() + 1) % 7
week_start = (today - timedelta(days=days_since_sunday)).strftime("%Y-%m-%d")
print(f"\nCurrent week_start: {week_start}")

conn.close()
