"""Presentation-only date formatting helpers for exported documents."""
import re
from datetime import datetime


_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?))?(?!\d)")


def format_export_dates(value):
    """Format ISO calendar dates as DD/MM/YYYY without changing stored values."""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    if not isinstance(value, str):
        return value

    def replace_date(match):
        try:
            formatted = datetime.strptime(
                f"{match.group(1)}-{match.group(2)}-{match.group(3)}", "%Y-%m-%d"
            ).strftime("%d/%m/%Y")
        except ValueError:
            return match.group(0)
        time_part = match.group(4)
        return formatted + (f" {time_part}" if time_part else "")

    return _ISO_DATE.sub(replace_date, value)
