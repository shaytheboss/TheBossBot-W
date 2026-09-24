"""Station audit: parsing Polymarket's resolution rules, judging, applying.

The intraday lock is only as good as the station it is confirmed against. In
the settled data some "impossible" NO bets lost by margins no rounding
explains — Seoul read 31°C and the 26°C bucket won — which is what a station
that differs from Polymarket's resolution station looks like.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.utils.station_audit import (
    ACTIONABLE, VERDICT_BOTH, VERDICT_NOT_WU, VERDICT_OK, VERDICT_PRIMARY,
    VERDICT_UNKNOWN, VERDICT_WU, apply_fix, extract_station_name,
    extract_wu_station, judge, other_source_domain,
)

LONDON_RULES = (
    "This market will resolve to the temperature range that contains the highest "
    "temperature recorded at the London City Airport Station in degrees Celsius on "
    "12 Aug '26. The resolution source for this market will be information from "
    "Wunderground, available here: https://www.wunderground.com/history/daily/gb/london/EGLC."
)


def _city(**kw):
    base = dict(id=1, name="London", primary_icao="EGLL", reference_icao="EGKK",
                wunderground_url="https://www.wunderground.com/history/daily/gb/england/heathrow/EGLL")
    base.update(kw)
    return SimpleNamespace(**base)


# ── Parsing ───────────────────────────────────────────────────────────────

class TestParsing:
    def test_the_icao_is_read_from_the_wunderground_link(self):
        assert extract_wu_station(LONDON_RULES) == (
            "EGLC", "https://www.wunderground.com/history/daily/gb/london/EGLC")

    def test_a_date_suffix_is_dropped(self):
        """The Wunderground collector appends its own /date/...; keeping
        Polymarket's would produce a doubled date path."""
        icao, url = extract_wu_station(
            "see https://www.wunderground.com/history/daily/kr/incheon/RKSI/date/2026-6-14")
        assert icao == "RKSI" and url.endswith("/RKSI")

    def test_trailing_punctuation(self):
        assert extract_wu_station(
            "(https://wunderground.com/history/daily/us/tx/houston/KHOU).")[0] == "KHOU"

    def test_a_lowercase_city_slug_is_not_a_station(self):
        """Station codes in Wunderground paths are upper-case; city slugs are
        not. Reading "rome" as station ROME would invent a fix."""
        assert extract_wu_station("https://www.wunderground.com/weather/it/rome") is None

    def test_the_code_directly_after_history_daily(self):
        """The first parser demanded a region segment before the code."""
        assert extract_wu_station("https://www.wunderground.com/history/daily/EHAM")[0] == "EHAM"

    def test_a_link_without_a_scheme(self):
        icao, url = extract_wu_station("see wunderground.com/history/daily/nl/schiphol/EHAM for data")
        assert icao == "EHAM" and url.startswith("https://")

    def test_a_link_cut_off_by_truncation_names_no_station(self):
        """Stored descriptions are capped at 500 characters. A half link must
        read as "no station" so the Gamma fallback runs — never as a guess."""
        assert extract_wu_station("https://www.wunderground.com/history/daily/nl/sch") is None

    def test_no_link_is_none(self):
        assert extract_wu_station("Resolves per the official observatory.") is None
        assert extract_wu_station(None) is None

    def test_the_station_name_is_read_too(self):
        assert extract_station_name(LONDON_RULES) == "London City Airport"

    def test_a_non_wunderground_source_is_named(self):
        text = "Resolves on data from https://www.weather.gov.hk/en/cis/climat.htm"
        assert other_source_domain(text) == "www.weather.gov.hk"

    def test_polymarket_links_are_not_mistaken_for_a_source(self):
        assert other_source_domain("see https://polymarket.com/rules") is None


# ── Judging ───────────────────────────────────────────────────────────────

class TestJudge:
    def _judge(self, city, rules=LONDON_RULES):
        return judge(city, extract_wu_station(rules), extract_station_name(rules),
                     other_source_domain(rules), "slug")

    def test_both_stations_wrong(self):
        a = self._judge(_city())
        assert a.verdict == VERDICT_BOTH and a.resolution_icao == "EGLC"

    def test_only_metar_wrong(self):
        a = self._judge(_city(wunderground_url="https://www.wunderground.com/history/daily/gb/london/EGLC"))
        assert a.verdict == VERDICT_PRIMARY

    def test_only_wunderground_wrong(self):
        a = self._judge(_city(primary_icao="EGLC"))
        assert a.verdict == VERDICT_WU

    def test_a_wunderground_url_with_no_station_counts_as_wrong(self):
        """Some cities were seeded with /weather/... URLs that name a city, not
        a station — the scraper then reads whatever Wunderground picks."""
        a = self._judge(_city(primary_icao="EGLC",
                              wunderground_url="https://www.wunderground.com/weather/gb/london"))
        assert a.verdict == VERDICT_WU and a.wu_url_icao is None

    def test_a_match_is_ok(self):
        a = self._judge(_city(primary_icao="EGLC",
                              wunderground_url="https://www.wunderground.com/history/daily/gb/london/EGLC"))
        assert a.verdict == VERDICT_OK

    def test_an_observatory_source_is_flagged_not_fixed(self):
        a = self._judge(_city(), "Resolves on https://www.weather.gov.hk/en/cis/climat.htm")
        assert a.verdict == VERDICT_NOT_WU and a.verdict not in ACTIONABLE
        assert "intraday" in a.advice

    def test_rules_that_mention_wunderground_are_never_called_not_wunderground(self):
        """The regression behind "47 of 48 do not match". Rules that name
        Wunderground but whose link could not be read were reported as
        resolving on www.weather.gov. A parsing gap is not a fact about the
        market: it must read as unknown, with nothing to apply."""
        text = ("Resolves on Wunderground data for the station (link unavailable). "
                "Background: https://www.weather.gov")
        a = judge(_city(), extract_wu_station(text), None, other_source_domain(text),
                  "slug", saw_wunderground=True)
        assert a.verdict == VERDICT_UNKNOWN
        assert a.verdict not in ACTIONABLE

    def test_nothing_readable_is_unknown(self):
        a = judge(_city(), None, None, None, None)
        assert a.verdict == VERDICT_UNKNOWN and a.verdict not in ACTIONABLE


# ── Applying ──────────────────────────────────────────────────────────────

class TestApply:
    def _audit(self, city):
        return judge(city, extract_wu_station(LONDON_RULES), None, None, "slug")

    def test_it_repoints_both_fields_and_keeps_the_old_station(self):
        city = _city(reference_icao=None)
        changes = apply_fix(city, self._audit(city))
        assert city.primary_icao == "EGLC"
        assert city.reference_icao == "EGLL", "the old primary is kept, not discarded"
        assert city.wunderground_url.endswith("/EGLC")
        assert set(changes) == {"primary_icao", "reference_icao", "wunderground_url"}

    def test_an_existing_reference_station_is_not_overwritten(self):
        city = _city(reference_icao="EGKK")
        apply_fix(city, self._audit(city))
        assert city.primary_icao == "EGLC" and city.reference_icao == "EGKK"

    def test_if_the_reference_was_the_right_one_they_swap(self):
        city = _city(primary_icao="EGLL", reference_icao="EGLC")
        apply_fix(city, self._audit(city))
        assert (city.primary_icao, city.reference_icao) == ("EGLC", "EGLL")

    def test_only_the_wrong_field_is_touched(self):
        city = _city(primary_icao="EGLC", reference_icao="EGKK")
        changes = apply_fix(city, self._audit(city))
        assert set(changes) == {"wunderground_url"}
        assert city.reference_icao == "EGKK"

    def test_nothing_actionable_refuses(self):
        city = _city()
        audit = judge(city, None, None, "www.weather.gov.hk", "slug")
        with pytest.raises(ValueError):
            apply_fix(city, audit)
        assert city.primary_icao == "EGLL", "a refused apply must change nothing"


# ── Problems in the city's own fields ─────────────────────────────────────

class TestFieldNotes:
    def test_a_placeholder_primary_icao_is_flagged(self):
        """Lucknow was found with primary_icao = "ICAO". Four upper-case
        letters, so no shape check catches it — and METAR was never fetched."""
        from app.utils.station_audit import field_notes
        notes = field_notes(_city(name="Lucknow", primary_icao="ICAO",
                                  wunderground_url="https://www.wunderground.com/history/daily/in/lucknow/VILK"))
        assert any("not a real station" in n for n in notes)

    def test_metar_and_wunderground_on_different_stations_is_flagged(self):
        """Seoul: METAR RKSS (Gimpo), Wunderground RKSI (Incheon). The
        'official max' then mixes two thermometers."""
        from app.utils.station_audit import field_notes
        notes = field_notes(_city(name="Seoul", primary_icao="RKSS",
                                  wunderground_url="https://www.wunderground.com/history/daily/kr/incheon/RKSI"))
        assert any("RKSS" in n and "RKSI" in n for n in notes)

    def test_a_wunderground_url_without_a_station_is_flagged(self):
        from app.utils.station_audit import field_notes
        notes = field_notes(_city(wunderground_url="https://www.wunderground.com/weather/tr/istanbul"))
        assert any("names no station" in n for n in notes)

    def test_a_consistent_city_has_no_notes(self):
        from app.utils.station_audit import field_notes
        assert field_notes(_city(primary_icao="EGLC",
                                 wunderground_url="https://www.wunderground.com/history/daily/gb/london/EGLC")) == []

    def test_notes_are_part_of_every_verdict(self):
        a = judge(_city(primary_icao="ICAO"), None, None, None, None)
        assert a.notes
