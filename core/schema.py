"""Database schema (core tables)."""
from core.db import db
from core.security import hash_password

def init_db():
    conn = db()
    c    = conn.cursor()

    # ── Users ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            full_name  TEXT,
            role       TEXT NOT NULL DEFAULT 'staff',
            store_name TEXT,
            is_active  INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (date('now'))
        )
    """)

    # ── Staff Profiles ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff_profiles (
            staff_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_number   INTEGER,
            first_name     TEXT NOT NULL,
            last_name      TEXT NOT NULL,
            store_name     TEXT,
            sex            TEXT,
            phone          TEXT,
            email          TEXT,
            address_1      TEXT,
            address_2      TEXT,
            address_3      TEXT,
            address_4      TEXT,
            postcode       TEXT,
            date_joined    TEXT,
            date_left      TEXT,
            leaving_reason TEXT,
            date_of_birth  TEXT,
            contracted_hrs REAL,
            hourly_rate    REAL,
            is_salaried    TEXT DEFAULT 'N',
            salary_amount  REAL,
            is_active      INTEGER DEFAULT 1
        )
    """)

    # ── Supplier Invoices (Retail) ──
    # NB: no UNIQUE(invoice_number, store_name) — different suppliers legitimately
    # reuse the same small invoice numbers; duplicate protection is the app's
    # save-time warning, not a hard DB rule.
    c.execute("""
        CREATE TABLE IF NOT EXISTS supplier_invoices (
            invoice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            seq_no         INTEGER,
            supplier_name  TEXT NOT NULL,
            store_name     TEXT NOT NULL,
            invoice_number TEXT,
            invoice_date   TEXT,
            gross_amount   REAL DEFAULT 0,
            vat_amount     REAL DEFAULT 0,
            net_amount     REAL DEFAULT 0,
            payment_terms  INTEGER,
            due_date       TEXT,
            paid_date      TEXT,
            amount_paid    REAL DEFAULT 0,
            credit_note    REAL DEFAULT 0,
            is_paid        TEXT DEFAULT 'No',
            payment_method TEXT,
            comments       TEXT,
            pdf_path          TEXT,
            approval_status   TEXT DEFAULT 'approved',
            submitted_by      TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            dd_statement_date TEXT,
            cheque_number     TEXT,
            accountant_sent_date TEXT,
            awaiting_invoice  TEXT,
            demand_ref        TEXT,
            linked_ref        TEXT,
            under_query       TEXT,
            updated_by        TEXT,
            updated_at        TEXT
        )
    """)

    # ── Property Invoices / Expenses ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS property_invoices (
            invoice_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            seq_no         INTEGER,
            property_name  TEXT NOT NULL,
            supplier_name  TEXT NOT NULL,
            invoice_number TEXT,
            invoice_date   TEXT,
            expense_type   TEXT,
            gross_amount   REAL DEFAULT 0,
            vat_amount     REAL DEFAULT 0,
            net_amount     REAL DEFAULT 0,
            due_date       TEXT,
            paid_date      TEXT,
            amount_paid    REAL DEFAULT 0,
            credit_note    REAL DEFAULT 0,
            is_paid        TEXT DEFAULT 'No',
            payment_method TEXT,
            cheque_number  TEXT,
            accountant_sent_date TEXT,
            awaiting_invoice TEXT,
            demand_ref     TEXT,
            linked_ref     TEXT,
            under_query    TEXT,
            comments       TEXT,
            pdf_path          TEXT,
            approval_status   TEXT DEFAULT 'approved',
            submitted_by      TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            updated_by        TEXT,
            updated_at        TEXT
        )
    """)

    # ── Rental Income ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS rental_income (
            record_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            property_name    TEXT NOT NULL,
            tenant_name      TEXT,
            rent_from        TEXT,
            rent_to          TEXT,
            agreed_rent      REAL DEFAULT 0,
            agency_comm      REAL DEFAULT 0,
            agency_vat       REAL DEFAULT 0,
            tds_fee          REAL DEFAULT 0,
            gas_elec_cert    REAL DEFAULT 0,
            inventory_fee    REAL DEFAULT 0,
            deposit_fee      REAL DEFAULT 0,
            tenancy_setup    REAL DEFAULT 0,
            repairs          REAL DEFAULT 0,
            repairs_vat      REAL DEFAULT 0,
            mortgage         REAL DEFAULT 0,
            net_rent         REAL DEFAULT 0,
            date_received    TEXT,
            notes            TEXT,
            created_at       TEXT DEFAULT (date('now'))
        )
    """)

    # ── Properties ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS properties (
            property_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            short_name     TEXT UNIQUE NOT NULL,
            full_address   TEXT NOT NULL,
            purchase_price REAL,
            mortgage       REAL,
            monthly_mortgage REAL,
            purchase_date  TEXT,
            first_rented   TEXT,
            is_active      INTEGER DEFAULT 1,
            notes          TEXT
        )
    """)

    # ── Rotas ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS store_rotas (
            rota_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_name TEXT NOT NULL,
            store_name TEXT NOT NULL,
            work_day   TEXT NOT NULL,
            shift_time TEXT DEFAULT 'OFF',
            UNIQUE(staff_name, store_name, work_day)
        )
    """)

    # ── Timesheets ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS timesheets (
            timesheet_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_name     TEXT NOT NULL,
            store_name     TEXT NOT NULL,
            work_date      TEXT NOT NULL,
            clock_in_time  TEXT,
            clock_out_time TEXT,
            status_flag    TEXT,
            absence_type   TEXT,
            comments       TEXT,
            UNIQUE(staff_name, store_name, work_date)
        )
    """)

    # ── Daily Sales ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_sales (
            sale_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name     TEXT NOT NULL,
            sale_date      TEXT NOT NULL,
            week_ending    TEXT,
            category       TEXT NOT NULL,
            amount         REAL DEFAULT 0,
            entered_by     TEXT,
            created_at     TEXT DEFAULT (datetime('now')),
            UNIQUE(store_name, sale_date, category)
        )
    """)

    # ── Sales Targets ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS sales_targets (
            target_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name     TEXT NOT NULL,
            year           INTEGER NOT NULL,
            month          INTEGER NOT NULL,
            target_amount  REAL DEFAULT 0,
            ly_actual      REAL DEFAULT 0,
            target_pct     REAL DEFAULT 1.05,
            UNIQUE(store_name, year, month)
        )
    """)

    # ── Supplier payment terms (owner-controlled; auto due-date on invoices) ──
    # term_type: 'days' (term_value = N days) or 'eom' (term_value = N months,
    # due = last day of the month N months after the invoice month). NULL term_type
    # = supplier captured but terms not set yet (shows as "needs terms").
    c.execute("""
        CREATE TABLE IF NOT EXISTS supplier_terms (
            supplier_name TEXT PRIMARY KEY,
            term_type     TEXT,
            term_value    INTEGER,
            updated_by    TEXT,
            updated_at    TEXT
        )
    """)

    # ── Sessions (server-side login tokens) ──
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        )
    """)

    # ── Seed default owner account (password: changeme) ──
    pw_hash = hash_password("changeme")
    c.execute("""
        INSERT OR IGNORE INTO users (username, password, full_name, role)
        VALUES ('owner', ?, 'Business Owner', 'owner')
    """, (pw_hash,))

    # ── Seed properties from known data ──
    for short, full, price, mort, m_mort, pdate in [
        ("104 Dane",  "104 Dane Road",      550000, 412482, 0,      "2021-05-07"),
        ("53 Ampth",  "53 Ampthill Way",    160000, 114906, 549.54, "2024-10-31"),
        ("26 Ampth",  "26 Ampthill Way",    165000, 123750, 0,      "2025-07-26"),
    ]:
        c.execute("""
            INSERT OR IGNORE INTO properties
                (short_name, full_address, purchase_price, mortgage, monthly_mortgage, purchase_date)
            VALUES (?,?,?,?,?,?)
        """, (short, full, price, mort, m_mort, pdate))

    # ── Lightweight migrations: add columns missing from older databases ──
    def ensure_columns(table, coldefs):
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, ddl in coldefs:
            if name not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    ensure_columns("supplier_invoices", [
        ("dd_statement_date",    "dd_statement_date TEXT"),
        ("cheque_number",        "cheque_number TEXT"),
        ("accountant_sent_date", "accountant_sent_date TEXT"),
        ("awaiting_invoice",     "awaiting_invoice TEXT"),
        ("demand_ref",           "demand_ref TEXT"),
        ("linked_ref",           "linked_ref TEXT"),
        ("under_query",          "under_query TEXT"),
        ("updated_by",           "updated_by TEXT"),
        ("updated_at",           "updated_at TEXT"),
    ])
    ensure_columns("property_invoices", [
        ("seq_no",               "seq_no INTEGER"),
        ("cheque_number",        "cheque_number TEXT"),
        ("accountant_sent_date", "accountant_sent_date TEXT"),
        ("awaiting_invoice",     "awaiting_invoice TEXT"),
        ("demand_ref",           "demand_ref TEXT"),
        ("linked_ref",           "linked_ref TEXT"),
        ("under_query",          "under_query TEXT"),
        ("updated_by",           "updated_by TEXT"),
        ("updated_at",           "updated_at TEXT"),
    ])

    conn.commit()
    conn.close()
    print("Database initialised.")
