"""Week/hours helpers shared by rota, timesheets and sales."""
from datetime import datetime, timedelta


def calc_paid_hours(raw_hrs: float) -> float:
    """Deduct 30 min unpaid break for shifts of 4 hours or more."""
    if not raw_hrs: return 0.0
    return round(raw_hrs - 0.5, 2) if raw_hrs >= 4.0 else round(raw_hrs, 2)


def parse_hours(start: str, end: str) -> float:
    """Calculate raw hours between two HH:MM time strings."""
    try:
        sh, sm = map(int, start.split(':'))
        eh, em = map(int, end.split(':'))
        return (eh*60+em - sh*60-sm) / 60
    except: return 0.0


def get_week_start(date_str: str = None) -> str:
    """Return Sunday of the week containing the given date (or today)."""
    d = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
    # Go back to Sunday
    days_since_sunday = d.weekday() + 1  # weekday() 0=Mon, so +1 for Sun
    if days_since_sunday == 7:
        days_since_sunday = 0
    return (d - timedelta(days=days_since_sunday)).strftime("%Y-%m-%d")


def get_week_dates(week_start: str) -> list:
    """Return list of 7 date strings for Sun-Sat week."""
    start = datetime.strptime(week_start, "%Y-%m-%d")
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
