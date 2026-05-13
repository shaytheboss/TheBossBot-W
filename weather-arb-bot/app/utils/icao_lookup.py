"""
Static lookup table for commonly monitored ICAO stations.
Provides coordinates and suggested reference/coastal stations.
"""

ICAO_DATA = {
    # San Francisco Bay Area
    "KSFO": {
        "name": "San Francisco Intl",
        "lat": 37.6213,
        "lon": -122.3790,
        "timezone": "America/Los_Angeles",
        "suggested_reference": "KHAF",
        "wunderground_url": "https://www.wunderground.com/weather/us/ca/san-francisco",
    },
    "KHAF": {
        "name": "Half Moon Bay",
        "lat": 37.5134,
        "lon": -122.5012,
        "timezone": "America/Los_Angeles",
    },
    # Los Angeles
    "KLAX": {
        "name": "Los Angeles Intl",
        "lat": 33.9425,
        "lon": -118.4081,
        "timezone": "America/Los_Angeles",
        "suggested_reference": "KSMO",
        "wunderground_url": "https://www.wunderground.com/weather/us/ca/los-angeles",
    },
    "KSMO": {
        "name": "Santa Monica Municipal",
        "lat": 34.0158,
        "lon": -118.4514,
        "timezone": "America/Los_Angeles",
    },
    # New York
    "KJFK": {
        "name": "John F. Kennedy Intl",
        "lat": 40.6413,
        "lon": -73.7781,
        "timezone": "America/New_York",
        "suggested_reference": "KISP",
        "wunderground_url": "https://www.wunderground.com/weather/us/ny/new-york-city",
    },
    "KLGA": {
        "name": "LaGuardia",
        "lat": 40.7769,
        "lon": -73.8740,
        "timezone": "America/New_York",
    },
    # Chicago
    "KORD": {
        "name": "Chicago O'Hare",
        "lat": 41.9742,
        "lon": -87.9073,
        "timezone": "America/Chicago",
        "wunderground_url": "https://www.wunderground.com/weather/us/il/chicago",
    },
    # Miami
    "KMIA": {
        "name": "Miami Intl",
        "lat": 25.7959,
        "lon": -80.2870,
        "timezone": "America/New_York",
        "wunderground_url": "https://www.wunderground.com/weather/us/fl/miami",
    },
}

BUOY_NEAREST = {
    "KSFO": "46026",
    "KLAX": "46025",
    "KJFK": "44025",
}


def lookup_icao(icao: str) -> dict:
    """Return known metadata for an ICAO code, or empty dict."""
    return ICAO_DATA.get(icao.upper(), {})


def suggest_reference(icao: str) -> str | None:
    data = ICAO_DATA.get(icao.upper(), {})
    return data.get("suggested_reference")


def nearest_buoy(icao: str) -> str | None:
    return BUOY_NEAREST.get(icao.upper())
