"""
Parse PIREP JSON/text records from the Aviation Weather API.
obsTime is returned as a Unix timestamp (int), not ISO string.
"""
import re
from datetime import datetime, timezone
from typing import Optional


def _parse_timestamp(value) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def parse_pirep_text(raw: str, json_record: Optional[dict] = None) -> Optional[dict]:
    if not raw:
        return None

    result: dict = {"raw": raw}

    tm = re.search(r"/TM\s+(\d{4})", raw)
    if tm:
        hhmm = tm.group(1)
        now = datetime.now(timezone.utc)
        try:
            observed_at = now.replace(
                hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0
            )
        except ValueError:
            observed_at = now
        result["observed_at"] = observed_at
    elif json_record:
        result["observed_at"] = _parse_timestamp(
            json_record.get("obsTime") or json_record.get("reportTime")
        )
    else:
        result["observed_at"] = datetime.now(timezone.utc)

    ov = re.search(r"/OV\s+([A-Z0-9 ]+?)(?:\s*/|$)", raw)
    if ov:
        result["location_offset"] = ov.group(1).strip()

    fl = re.search(r"/FL(\d+)", raw)
    if fl:
        result["flight_level_ft"] = int(fl.group(1)) * 100

    tp = re.search(r"/TP\s+([A-Z0-9]+)", raw)
    if tp:
        result["aircraft_type"] = tp.group(1)

    ta = re.search(r"/TA\s+([+-]?\d+)", raw)
    if ta:
        result["temperature_c"] = float(ta.group(1))

    wv = re.search(r"/WV\s+(\d{3})(\d{2,3})KT", raw)
    if wv:
        result["wind_direction"] = int(wv.group(1))
        result["wind_speed_kt"] = int(wv.group(2))

    tb = re.search(r"/TB\s+([A-Z0-9 ]+?)(?:\s*/|$)", raw)
    if tb:
        result["turbulence"] = tb.group(1).strip()[:20]

    ic = re.search(r"/IC\s+([A-Z0-9 ]+?)(?:\s*/|$)", raw)
    if ic:
        result["icing"] = ic.group(1).strip()[:20]

    if len(result) <= 2:
        return None

    return result
