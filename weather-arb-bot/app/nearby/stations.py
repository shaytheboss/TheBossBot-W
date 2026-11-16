"""Nearby METAR stations around each settlement station — data, not logic.

Keyed by the SETTLEMENT station (City.primary_icao), so a city whose station
is changed simply has no neighbours until this table is regenerated: the
neighbours of the old station are never attached to the new one.

Generated 2026-11-16 from the Iowa Environmental Mesonet station registry:
active stations 3-80 km from the settlement station, nearest first, keeping
only those that published a METAR on aviationweather.gov in the previous six
hours, at most five per station. Each entry: (icao, distance_km, bearing_deg
from the settlement station, 0 = north). Cities with no such station (parts of
China, Cape Town, Jeddah, Ankara, Helsinki, ...) are simply absent.
"""

NEARBY: dict[str, tuple[tuple[str, float, int], ...]] = {
    "EHAM": (  # Amsterdam
        ("EHRD", 46.6, 210),
        ("EHQS", 49.5, 347),
        ("EHQW", 49.6, 347),
        ("EHQN", 50.2, 348),
        ("EHLE", 52.5, 72),
    ),
    "KATL": (  # Atlanta
        ("KFTY", 18.2, 337),
        ("KHMP", 28.6, 159),
        ("KPDK", 30.2, 25),
        ("KMGE", 32.5, 348),
        ("KFFC", 32.7, 201),
    ),
    "KAUS": (  # Austin
        ("KATT", 17.2, 333),
        ("KEDC", 25.9, 26),
        ("KHYI", 36.8, 209),
        ("KRYW", 44.4, 322),
        ("KGTU", 55.4, 0),
    ),
    "ZBAA": (  # Beijing
        ("ZBAD", 64.6, 193),
    ),
    "SAEZ": (  # Buenos Aires
        ("SADM", 18.9, 329),
        ("SADP", 24.6, 343),
        ("SABE", 31.2, 21),
        ("SADJ", 37.3, 322),
        ("SADF", 41.3, 353),
    ),
    "ZUUU": (  # Chengdu
        ("ZUTF", 56.4, 133),
    ),
    "KORD": (  # Chicago
        ("KPWK", 18.0, 7),
        ("KMDW", 24.4, 143),
        ("KDPA", 26.9, 257),
        ("KLOT", 41.5, 199),
        ("KARR", 50.1, 245),
    ),
    "KDAL": (  # Dallas
        ("KADS", 13.6, 6),
        ("KDFW", 18.2, 288),
        ("KRBD", 18.7, 185),
        ("KGPM", 24.6, 228),
        ("KGKY", 30.5, 228),
    ),
    "KBKF": (  # Denver
        ("KDEN", 16.7, 29),
        ("KAPA", 16.9, 210),
        ("KCFO", 20.5, 63),
        ("KBJC", 38.8, 306),
        ("KEIK", 42.6, 324),
    ),
    "KHOU": (  # Houston
        ("KEFD", 12.4, 106),
        ("KLVJ", 13.8, 163),
        ("KMCJ", 13.8, 308),
        ("KAXH", 23.8, 232),
        ("KSGR", 36.2, 267),
    ),
    "LTFM": (  # Istanbul
        ("LTBA", 32.4, 169),
        ("LTFJ", 62.5, 130),
        ("LTBU", 70.2, 259),
    ),
    "WMKK": (  # Kuala Lumpur
        ("WMSA", 48.0, 344),
        ("WMKM", 79.3, 129),
    ),
    "EGLL": (  # London
        ("EGWU", 8.7, 20),
        ("EGLF", 30.9, 224),
        ("EGLC", 35.9, 85),
        ("EGKB", 38.1, 116),
        ("EGKK", 41.3, 153),
    ),
    "KLAX": (  # Los Angeles
        ("KHHR", 5.0, 110),
        ("KSMO", 10.8, 329),
        ("KTOA", 15.6, 164),
        ("KLGB", 26.2, 122),
        ("KBUR", 29.3, 5),
    ),
    "LEMD": (  # Madrid
        ("LETO", 9.1, 78),
        ("LEVS", 22.1, 243),
        ("LEGT", 23.3, 217),
        ("LECV", 31.2, 326),
    ),
    "MMMX": (  # Mexico City
        ("MMSM", 35.9, 9),
        ("MMTO", 53.0, 258),
        ("MMCB", 62.8, 196),
    ),
    "KMIA": (  # Miami
        ("KOPF", 14.0, 14),
        ("KHWO", 24.7, 18),
        ("KHST", 34.0, 191),
        ("KFLL", 35.8, 26),
        ("KFXE", 48.1, 18),
    ),
    "LIMC": (  # Milan
        ("LIMN", 11.9, 201),
        ("LIML", 46.0, 114),
        ("LIME", 76.1, 87),
    ),
    "EDDM": (  # Munich
        ("ETSI", 44.6, 333),
        ("EDMO", 49.7, 232),
        ("ETSN", 58.9, 312),
        ("EDMA", 64.4, 277),
        ("ETSL", 73.5, 255),
    ),
    "KLGA": (  # New York
        ("KNYC", 7.5, 270),
        ("KJRB", 13.9, 231),
        ("KJFK", 18.6, 148),
        ("KHPN", 35.1, 24),
        ("KFRG", 39.7, 98),
    ),
    "MPMG": (  # Panama City
        ("MPPA", 11.9, 230),
        ("MPTO", 18.1, 66),
    ),
    "LFPB": (  # Paris
        ("LFPG", 9.5, 56),
        ("LFPV", 27.1, 218),
        ("LFPO", 28.0, 187),
        ("LFPT", 31.6, 297),
        ("LFPN", 33.3, 224),
    ),
    "KSFO": (  # San Francisco
        ("KHAF", 16.1, 223),
        ("KSQL", 16.3, 137),
        ("KOAK", 16.6, 49),
        ("KHWD", 22.8, 79),
        ("KPAO", 28.9, 127),
    ),
    "KSEA": (  # Seattle
        ("KRNT", 9.2, 54),
        ("KBFI", 9.5, 7),
        ("KTIW", 27.9, 225),
        ("KPWT", 34.2, 278),
        ("KTCM", 36.3, 200),
    ),
    "RKSI": (  # Seoul
        ("RKSS", 31.7, 71),
        ("RKSM", 58.6, 92),
        ("RKSO", 66.2, 129),
        ("RKSG", 76.1, 138),
    ),
    "ZSPD": (  # Shanghai
        ("ZSSS", 41.9, 282),
    ),
    "WSSS": (  # Singapore
        ("WSAP", 8.2, 265),
        ("WSSL", 16.7, 290),
    ),
    "SBGR": (  # São Paulo
        ("SBMT", 19.2, 243),
        ("SBSP", 28.8, 221),
        ("SBST", 57.9, 161),
        ("SBSJ", 66.1, 70),
    ),
    "RCSS": (  # Taipei
        ("RCTP", 32.1, 272),
    ),
    "RJTT": (  # Tokyo
        ("RJTR", 35.3, 263),
        ("RJTY", 44.7, 299),
        ("RJAA", 59.5, 67),
    ),
    "CYYZ": (  # Toronto
        ("CXTO", 17.1, 96),
        ("CYTZ", 19.7, 106),
        ("CWWB", 43.9, 197),
        ("CXHM", 48.8, 207),
        ("CXVN", 58.0, 161),
    ),
    "EPWA": (  # Warsaw
        ("EPMO", 38.3, 327),
    ),
}


def neighbours(icao: str | None) -> tuple[str, ...]:
    return tuple(s for s, _, _ in NEARBY.get((icao or "").upper(), ()))
