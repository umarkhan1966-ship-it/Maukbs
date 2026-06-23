"""Shared constants."""

STORE_GPS = {
    "Uxbridge": (51.5462, -0.4791),
    "Newbury":  (51.4014, -1.3231)
}


GEOFENCE_RADIUS_M = 200


SALES_CATS = [
    "Digital Printing", "Other D&P", "Instant Prints", "Reprint/Enlarge",
    "Internet Orders", "Passport", "Film Media", "Graphic Design",
    "Large Format", "Toner/ Laser Output", "Batteries", "Frames & Albums",
    "Photogifts", "Backup to Media", "DVD Transfer", "Studio",
    "Sundry", "Promotions", "RCS (STD VAT)", "RCS (ZERO)",
    "Photobooks", "TYPE B Sales"
]


DAYS = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]

FULL_DAYS = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]


# Absence / leave codes shown on the rota and leave screens
ABSENCE_TYPES = {
    "H":  "Holiday",
    "S":  "Sick",
    "B":  "Bank Holiday",
    "L":  "Lateness",
    "AL": "Authorised Leave",
    "UL": "Unauthorised Leave",
    "MA": "Maternity",
    "PA": "Paternity",
    "JP": "Jury Service",
    "TO": "TOIL",
    "WFH":"Working From Home",
}
