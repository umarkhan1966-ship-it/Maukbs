import sqlite3
conn = sqlite3.connect("business_vault.db")
cur  = conn.cursor()

migrations = [
    "ALTER TABLE staff_documents ADD COLUMN is_current INTEGER DEFAULT 1",
    "ALTER TABLE staff_documents ADD COLUMN generated INTEGER DEFAULT 0",
    "ALTER TABLE staff_documents ADD COLUMN file_name TEXT",
    "ALTER TABLE staff_documents ADD COLUMN uploaded_by TEXT",
    "ALTER TABLE staff_documents ADD COLUMN notes TEXT",
    "ALTER TABLE staff_documents ADD COLUMN version INTEGER DEFAULT 1",
    "ALTER TABLE document_templates ADD COLUMN file_name TEXT",
    "ALTER TABLE document_templates ADD COLUMN uploaded_by TEXT",
    "ALTER TABLE document_templates ADD COLUMN notes TEXT",
]

for sql in migrations:
    try:
        cur.execute(sql)
        print(f"✅ {sql[:50]}")
    except Exception as e:
        print(f"⚠️  Already exists: {sql[:50]}")

conn.commit()
conn.close()
print("\n✅ Done")
