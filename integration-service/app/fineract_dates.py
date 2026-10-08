from datetime import date

FINERACT_DATE_FORMAT = "dd MMMM yyyy"


def format_fineract_date(value: date) -> str:
    """Fineract's APIs expect dates as e.g. "15 January 2026" with a matching
    `dateFormat` parameter -- this is that exact format, in one place rather
    than re-implemented per call site."""
    return value.strftime("%d %B %Y")
