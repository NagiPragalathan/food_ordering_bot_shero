"""Distance calculation.

Pure maths, no I/O, so serviceability decisions are deterministic and easy to
test. Straight-line (haversine) distance is the default; road distance via the
Google Distance Matrix is available when a key is configured, since a 3 km
straight line can be a 7 km drive across a river.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lon2 - lon1)

    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def round_km(value: float) -> float:
    """One decimal place - the precision the customer is shown ('3.2 km')."""
    return round(value, 1)
