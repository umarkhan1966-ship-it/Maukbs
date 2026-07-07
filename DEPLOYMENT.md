# BusinessVault — Deployment Guide (Railway)

How to host BusinessVault so both stores can reach it over the internet, with
HTTPS. Written to be followed step by step.

---

## 1. What the code already does (deployment-ready)

These are handled in the code, so you don't need to think about them:

- **Files live on a persistent volume.** The database, invoice PDFs and staff
  documents are written under `DATA_DIR`. Set `DATA_DIR=/data` on the host and
  point a volume at `/data` — then redeploys never wipe your data.
- **Start command** — `Procfile`: `uvicorn app:app --host 0.0.0.0 --port $PORT`.
- **Python version** — pinned to 3.12 via `.python-version`.
- **Secure sessions** — set `SECURE_COOKIES=1` and the login cookie is sent
  HTTPS-only.
- **Brute-force guard** — after 8 failed logins from one IP within 15 minutes,
  that IP is blocked for a cool-off period.

---

## 2. Environment variables to set on Railway

| Variable          | Value    | Why |
|-------------------|----------|-----|
| `DATA_DIR`        | `/data`  | Put the database + files on the persistent volume |
| `SECURE_COOKIES`  | `1`      | HTTPS-only session cookie |
| `PORT`            | *(auto)* | Railway sets this itself — do **not** set it manually |

---

## 3. Railway setup (dashboard steps)

1. **Create a Railway account** and a new **Project**.
2. **Deploy from GitHub** — connect the BusinessVault repo. Railway auto-detects
   Python (from `requirements.txt`) and uses the `Procfile` to start it.
3. **Region:** choose **Europe (EU West / Amsterdam)** — the data is UK
   financial + staff records; keep it in Europe.
4. **Add a Volume** to the service and set its **mount path to `/data`**.
5. **Add the environment variables** from section 2.
6. **Keep it to a single instance** (replicas = 1). This app uses SQLite, which
   must run on exactly one instance — do not enable horizontal scaling.
7. **Deploy.** On first boot it creates an empty database on the volume.
8. (Optional, later) Add a **custom domain** — Railway gives HTTPS on both its
   own `*.up.railway.app` address and any custom domain.

---

## 4. First-deploy data migration (IMPORTANT)

The repo does **not** contain your data (the database and PDFs are deliberately
kept out of git). So after the first deploy the app starts **empty**. You must
copy your existing data onto the `/data` volume **once**:

- `business_vault.db`  → `/data/business_vault.db`
- `invoice_pdfs/`      → `/data/invoice_pdfs/`

This is done with the **Railway CLI** (upload into the running volume) or a
one-off transfer. **Claude will walk you through this at deploy time** — it's a
careful, one-time step, and we take a fresh backup of the local database first.

After copying, restart the service and confirm the invoice counts match
(Uxbridge 2,792 / Newbury 2,693 as last reconciled).

---

## 5. Security checklist — before you tell staff it's live

- [ ] `SECURE_COOKIES=1` is set and the site loads over **https://**
- [ ] **Owner** password changed off the default (via My Profile → Change Password)
- [ ] **Staff** passwords reset and handed out (Manage Users → Reset)
- [ ] **teststaff** account disabled or deleted
- [ ] Manager accounts left disabled unless you want them
- [ ] You've logged in from a phone/other network to confirm it's reachable

---

## 6. Backups (must be set up before real go-live)

`scripts/backup.py` makes a safe, timestamped snapshot of the database + files.

- **Schedule it** as a Railway **Cron** service (e.g. daily):
  `python scripts/backup.py`
- **Off-site copy — still to decide.** A backup on the same volume is *not*
  safe. Pick an independent destination (e.g. **Backblaze B2** or **AWS S3**) and
  we'll wire the script to upload each archive there. This is the one remaining
  choice before go-live.
- **Test a restore** at least once — an untested backup isn't a backup.

---

## 7. Known follow-ups (not blocking invoices go-live)

- **Staff document templates** (`doc_templates/*.dotx`) are git-ignored and won't
  deploy. They're only needed when the staff contract/letter feature goes live —
  upload them to the volume (or un-ignore them) at that point.
- **Two-factor login** or restricting access to the stores' IP addresses — optional
  extra hardening if you ever want it tighter.
- **Postgres** — only if you outgrow SQLite; not needed at current scale.
