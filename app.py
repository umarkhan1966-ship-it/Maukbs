"""
BusinessVault — application entry point.

Thin composition root: builds the FastAPI app, initialises the database
schema, and mounts each feature module's router. All routes live in
modules/; shared helpers live in core/.
"""
from fastapi import FastAPI
from core import schema

app = FastAPI()
schema.init_db()

# NOTE: `rota` and `timesheets` are the OLD cloud-build modules — retired 2026-08-03
# pending a proper rebuild (real clock-in/out + scheduling) inside the staff module.
# Their files remain dormant (not imported/routed) and their tables were cleared.
from modules import auth, general, profile, invoices, staff, sales, users_admin

for _mod in (auth, general, profile, invoices, staff, sales, users_admin):
    app.include_router(_mod.router)
