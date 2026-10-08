from datetime import date

FINERACT_DATE_FORMAT = "dd MMMM yyyy"


def format_fineract_date(value: date) -> str:
    """Fineract's APIs expect dates as e.g. "15 January 2026" with a matching
    `dateFormat` parameter -- this is that exact format, in one place rather
    than re-implemented per call site."""
    return value.strftime("%d %B %Y")


def parse_fineract_date_array(value: list[int]) -> date:
    """The reverse direction: Fineract's own JSON responses represent a date
    as `[year, month, day]` (confirmed live, e.g. a transaction's "date"
    field), not a string -- needed when deriving a journal entry's
    `entry_date` from a Fineract transaction response."""
    year, month, day = value
    return date(year, month, day)
