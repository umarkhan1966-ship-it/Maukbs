"""HTML page shell."""

def page(title: str, content: str, user: dict, active: str = "") -> str:
    role     = user.get("role", "staff")
    name     = user.get("full_name") or user.get("username", "")
    store    = user.get("store_name") or ""
    is_owner = role == "owner"
    is_mgr   = role in ("owner", "manager")

    # Nav items: (label, href, icon, min_role)
    nav = [
        ("Dashboard",   "/",              "&#11035;", "staff"),
        ("My Profile",  "/my-profile",    "&#128100;","staff"),
        ("Sales",       "/sales",         "&#128200;","staff"),
        ("Invoices",    "/invoices",      "&#129534;","staff"),
        ("Staff",       "/staff",         "&#128100;","manager"),
        ("Rota",        "/rota",          "&#128197;","manager"),
        ("Timesheets",  "/timesheets",    "&#9200;",  "manager"),
        ("Property",    "/property",      "&#127968;","owner"),
        ("Settings",    "/settings",      "&#9881;",  "owner"),
    ]

    nav_html = ""
    for label, href, icon, min_role in nav:
        if min_role == "owner"   and not is_owner: continue
        if min_role == "manager" and not is_mgr:   continue
        active_cls = "bg-white/15 font-black" if active == label.lower() else "hover:bg-white/10"
        nav_html += f"<a href='{href}' class='flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-semibold transition {active_cls}'>{icon} {label}</a>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — BusinessVault</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;900&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
  <style>
    body {{ font-family: 'DM Sans', sans-serif; }}
    .mono {{ font-family: 'DM Mono', monospace; }}
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: #f1f5f9; }}
    ::-webkit-scrollbar-thumb {{ background: #cbd5e1; border-radius: 3px; }}
    .card {{ background: white; border-radius: 16px; border: 1px solid #e2e8f0; padding: 24px; }}
    .btn-primary {{ background:#1e3a5f; color:white; font-weight:700; padding:8px 20px; border-radius:10px; font-size:14px; transition:all .15s; display:inline-block; }}
    .btn-primary:hover {{ background:#16304f; }}
    .btn-secondary {{ background:#f1f5f9; color:#334155; font-weight:700; padding:8px 20px; border-radius:10px; font-size:14px; transition:all .15s; display:inline-block; }}
    .btn-secondary:hover {{ background:#e2e8f0; }}
    .btn-danger {{ background:#fee2e2; color:#dc2626; font-weight:700; padding:8px 20px; border-radius:10px; font-size:14px; transition:all .15s; display:inline-block; }}
    .btn-danger:hover {{ background:#fecaca; }}
    .btn-success {{ background:#dcfce7; color:#16a34a; font-weight:700; padding:8px 20px; border-radius:10px; font-size:14px; transition:all .15s; display:inline-block; }}
    .btn-success:hover {{ background:#bbf7d0; }}
    .badge-paid {{ background:#dcfce7; color:#16a34a; font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; }}
    .badge-overdue {{ background:#fee2e2; color:#dc2626; font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; }}
    .badge-partial {{ background:#fef3c7; color:#d97706; font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; }}
    .badge-unpaid {{ background:#f1f5f9; color:#64748b; font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; }}
    .tbl {{ width:100%; border-collapse:collapse; font-size:13px; }}
    .tbl th {{ background:#0f2942; color:white; padding:10px 12px; text-align:left; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; white-space:nowrap; }}
    .tbl td {{ padding:10px 12px; border-bottom:1px solid #f1f5f9; vertical-align:middle; }}
    .tbl tr:hover td {{ background:#f8fafc; }}
    .tbl tr:last-child td {{ border-bottom:none; }}
    input, select, textarea {{
      width:100%; border:1px solid #e2e8f0; border-radius:8px;
      padding:8px 12px; font-size:14px; font-family:'DM Sans',sans-serif;
      outline:none; transition:border .15s; background:white;
    }}
    input:focus, select:focus, textarea:focus {{ border-color:#1e3a5f; }}
    input[type=number]::-webkit-outer-spin-button,
    input[type=number]::-webkit-inner-spin-button {{ -webkit-appearance:none; margin:0; }}
    input[type=number] {{ -moz-appearance:textfield; }}
    label {{ font-size:12px; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:.05em; display:block; margin-bottom:4px; }}
    .flash-success {{ background:#dcfce7; border:1px solid #86efac; color:#15803d; padding:12px 16px; border-radius:10px; font-size:14px; font-weight:600; }}
    .flash-error   {{ background:#fee2e2; border:1px solid #fca5a5; color:#dc2626; padding:12px 16px; border-radius:10px; font-size:14px; font-weight:600; }}
  </style>
</head>
<body class="bg-slate-100 min-h-screen">

  <!-- Sidebar -->
  <div class="fixed top-0 left-0 h-full w-52 z-40"
       style="background:linear-gradient(180deg,#0f2942 0%,#1e3a5f 100%);">
    <div class="p-5 border-b border-white/10">
      <div class="text-white font-black text-lg tracking-tight">BusinessVault</div>
      <div class="text-blue-300 text-xs font-semibold mt-0.5">Maukbs Ltd</div>
    </div>
    <nav class="p-3 space-y-1 text-white">
      {nav_html}
    </nav>
    <div class="absolute bottom-0 left-0 right-0 p-4 border-t border-white/10">
      <div class="text-white text-xs font-bold truncate">{name}</div>
      <div class="text-blue-300 text-xs capitalize">{role}{' · ' + store if store else ''}</div>
      <a href="/logout" class="text-blue-300 hover:text-white text-xs mt-1 inline-block transition">Sign out →</a>
    </div>
  </div>

  <!-- Main content -->
  <div class="ml-52 min-h-screen">
    <div class="max-w-7xl mx-auto p-6 space-y-6">
      {content}
    </div>
  </div>

</body>
</html>"""
