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

from modules import auth, general, profile, invoices, staff, rota, timesheets, sales, users_admin

for _mod in (auth, general, profile, invoices, staff, rota, timesheets, sales, users_admin):
    app.include_router(_mod.router)
