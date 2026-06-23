"""general routes."""
import os, io, re, uuid, math, shutil, secrets, hashlib
from datetime import datetime, timedelta, date
from fastapi import APIRouter, Request, Form, Cookie, UploadFile, File
from fastapi.responses import (HTMLResponse, RedirectResponse, FileResponse,
                               JSONResponse, StreamingResponse, Response,
                               PlainTextResponse)
from core.db import DB_FILE, db, q
from core.constants import *
from core.security import (hash_password, verify_password,
                           get_session, require_login)
from core.layout import page
from core.rota_utils import (calc_paid_hours, parse_hours,
                             get_week_start, get_week_dates)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(session: str | None = Cookie(default=None)):
    redir, user = require_login(session)
    if redir: return redir
    today    = datetime.now().strftime("%A, %d %B %Y")
    is_owner = user["role"] == "owner"

    # Quick summary counts
    overdue  = q("""SELECT COUNT(*) as n FROM supplier_invoices
                    WHERE is_paid!='Yes' AND due_date < date('now')""", fetch=True)
    overdue_n = overdue[0]["n"] if overdue else 0

    overdue_val = q("""SELECT COALESCE(SUM(gross_amount-amount_paid-credit_note),0) as v
                       FROM supplier_invoices
                       WHERE is_paid!='Yes' AND due_date < date('now')""", fetch=True)
    overdue_v = overdue_val[0]["v"] if overdue_val else 0

    active_staff = q("SELECT COUNT(*) as n FROM staff_profiles WHERE is_active=1", fetch=True)
    staff_n = active_staff[0]["n"] if active_staff else 0

    # This week's sales (both stores)
    week_sales = q("""SELECT COALESCE(SUM(amount),0) as v FROM daily_sales
                      WHERE sale_date >= date('now','-7 days')""", fetch=True)
    week_v = week_sales[0]["v"] if week_sales else 0

    def stat_card(icon, label, value, sub, colour):
        return f"""
        <div class='card flex items-start gap-4'>
          <div class='text-3xl'>{icon}</div>
          <div>
            <div class='text-xs font-bold text-slate-400 uppercase tracking-wide'>{label}</div>
            <div class='text-2xl font-black' style='color:{colour}'>{value}</div>
            <div class='text-xs text-slate-400 mt-0.5'>{sub}</div>
          </div>
        </div>"""

    cards = f"""
    <div>
      <div class='text-2xl font-black text-slate-800'>Good {'morning' if datetime.now().hour < 12 else 'afternoon'}, {user['full_name'] or user['username'].title()} 👋</div>
      <div class='text-slate-400 text-sm mt-1'>{today}</div>
    </div>
    <div class='grid grid-cols-2 gap-4' style='grid-template-columns:repeat(auto-fit,minmax(200px,1fr))'>
      {stat_card('🚨', 'Overdue Invoices', overdue_n, f'£{overdue_v:,.2f} outstanding', '#dc2626')}
      {stat_card('📈', 'Sales This Week', f'£{week_v:,.2f}', 'Both stores combined', '#16a34a')}
      {stat_card('👤', 'Active Staff', staff_n, 'Across both stores', '#1e3a5f')}
      {stat_card('🏠', 'Properties', '3', '104 Dane · 53 Ampth · 26 Ampth', '#7c3aed') if is_owner else ''}
    </div>

    <div class='grid gap-4' style='grid-template-columns:repeat(auto-fit,minmax(300px,1fr))'>
      <div class='card'>
        <div class='font-black text-slate-700 mb-3'>⚡ Quick Actions</div>
        <div class='space-y-2'>
          <a href='/invoices' class='btn-primary block text-center'>🧾 Manage Invoices</a>
          <a href='/sales' class='btn-primary block text-center'>📈 Enter Today's Sales</a>
          <a href='/rota' class='btn-secondary block text-center'>📅 View Rota</a>
          {'<a href="/property" class="btn-secondary block text-center">🏠 Property Portfolio</a>' if is_owner else ''}
        </div>
      </div>
      <div class='card'>
        <div class='font-black text-slate-700 mb-3'>📋 Modules</div>
        <div class='text-sm text-slate-500 space-y-2'>
          <div class='flex justify-between items-center py-1 border-b border-slate-100'>
            <span>🧾 Invoice Management</span><span class='badge-paid'>Ready</span>
          </div>
          <div class='flex justify-between items-center py-1 border-b border-slate-100'>
            <span>📈 Sales & Franchise</span><span class='badge-unpaid'>Coming next</span>
          </div>
          <div class='flex justify-between items-center py-1 border-b border-slate-100'>
            <span>👤 Staff & Rota</span><span class='badge-unpaid'>Coming soon</span>
          </div>
          <div class='flex justify-between items-center py-1'>
            <span>🏠 Property Portfolio</span><span class='badge-unpaid'>Coming soon</span>
          </div>
        </div>
      </div>
    </div>"""

    return page("Dashboard", cards, user, "dashboard")


def placeholder(title, icon, session):
    redir, user = require_login(session)
    if redir: return redir
    content = f"""
    <div class='text-2xl font-black text-slate-800'>{icon} {title}</div>
    <div class='card text-center py-16 text-slate-400'>
      <div class='text-4xl mb-3'>🚧</div>
      <div class='font-bold text-lg'>Coming in the next build</div>
      <div class='text-sm mt-1'>This module is being built now</div>
    </div>"""
    return page(title, content, user, title.lower())


@router.get("/property",   response_class=HTMLResponse)
def property_page(session: str | None = Cookie(default=None)):
    return placeholder("Property", "🏠", session)


@router.get("/settings",   response_class=HTMLResponse)
def settings_page(session: str | None = Cookie(default=None)):
    return placeholder("Settings", "⚙️", session)
