"""Timestamped hot-backup of the database + uploaded files.

Safe to run while the app is live: the database is copied via SQLite's online
backup API (a consistent snapshot), then bundled with the invoice PDFs and any
staff documents into a single .tar.gz.

Run locally:      python scripts/backup.py
On the host:      schedule it (e.g. a Railway Cron service) — see DEPLOYMENT.md.

IMPORTANT: a backup that sits on the same volume as the live data is NOT safe.
The final step (copying the archive to independent off-site storage) still needs
a destination chosen — see DEPLOYMENT.md.
"""
import os
import sqlite3
import tarfile
import datetime

DATA_DIR   = os.environ.get("DATA_DIR", "")
BACKUP_DIR = os.environ.get("BACKUP_DIR") or (
    os.path.join(DATA_DIR, "backups") if DATA_DIR else "backups")


def dpath(*parts):
    return os.path.join(DATA_DIR, *parts) if DATA_DIR else os.path.join(*parts)


def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1) Consistent DB snapshot (safe even while the app is writing).
    db_src = dpath("business_vault.db")
    snap   = os.path.join(BACKUP_DIR, f"_snap_{ts}.db")
    if os.path.exists(db_src):
        src = sqlite3.connect(db_src)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()

    # 2) Bundle the snapshot + uploaded files into one archive.
    archive = os.path.join(BACKUP_DIR, f"businessvault_{ts}.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        if os.path.exists(snap):
            tar.add(snap, arcname="business_vault.db")
        for folder in ("invoice_pdfs", "staff_docs"):
            p = dpath(folder)
            if os.path.isdir(p):
                tar.add(p, arcname=folder)

    if os.path.exists(snap):
        os.remove(snap)

    print("Backup written:", archive)
    # NEXT STEP (DEPLOYMENT.md): copy `archive` OFF the host to independent
    # storage (e.g. Backblaze B2 / AWS S3). Same-volume backups are not safe.


if __name__ == "__main__":
    main()
