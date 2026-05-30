"""Pure math utilities: haversine, great-circle interpolation, wx grid lookup."""
from __future__ import annotations

import math
import numpy as np

# ── Haversine ──────────────────────────────────────────────────────────────────

EARTH_NM = 3440.065  # Earth radius in nautical miles


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles between two lat/lon points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_NM * math.asin(math.sqrt(a))


def cum_dist_nm(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Cumulative great-circle distance array along a sequence of waypoints.

    Result[0] == 0; Result[-1] == total route length in NM.
    """
    n = len(lats)
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        out[i] = out[i - 1] + haversine_nm(lats[i - 1], lons[i - 1], lats[i], lons[i])
    return out


def interpolate_position(
    lats: np.ndarray,
    lons: np.ndarray,
    cum_nm: np.ndarray,
    target_nm: float,
) -> tuple[float, float]:
    """Linear interpolation of lat/lon at a given cumulative distance along the route."""
    total = cum_nm[-1]
    target_nm = max(0.0, min(total, target_nm))

    # Binary search for the bracketing segment
    idx = int(np.searchsorted(cum_nm, target_nm, side="right")) - 1
    idx = max(0, min(idx, len(cum_nm) - 2))

    seg_len = cum_nm[idx + 1] - cum_nm[idx]
    if seg_len < 1e-9:
        return float(lats[idx]), float(lons[idx])

    frac = (target_nm - cum_nm[idx]) / seg_len
    lat = lats[idx] + frac * (lats[idx + 1] - lats[idx])
    lon = lons[idx] + frac * (lons[idx + 1] - lons[idx])
    return float(lat), float(lon)


# ── Weather grid ───────────────────────────────────────────────────────────────

WX_LAT_MIN = 21.943
WX_LAT_MAX = 55.7765
WX_LON_MIN = -135.0
WX_LON_MAX = -67.5
WX_ROWS = 256
WX_COLS = 358


def latlon_to_pixel(lat: float, lon: float) -> tuple[int, int]:
    """Map a lat/lon to the nearest (row, col) index in the wx grid."""
    i = int((WX_LAT_MAX - lat) / (WX_LAT_MAX - WX_LAT_MIN) * WX_ROWS)
    j = int((lon - WX_LON_MIN) / (WX_LON_MAX - WX_LON_MIN) * WX_COLS)
    i = max(0, min(WX_ROWS - 1, i))
    j = max(0, min(WX_COLS - 1, j))
    return i, j


def pixel_bbox_latlon(i: int, j: int) -> tuple[float, float, float, float]:
    """Return (lat_north, lat_south, lon_west, lon_east) for a wx grid pixel."""
    cell_h = (WX_LAT_MAX - WX_LAT_MIN) / WX_ROWS
    cell_w = (WX_LON_MAX - WX_LON_MIN) / WX_COLS
    lat_n = WX_LAT_MAX - i * cell_h
    lat_s = lat_n - cell_h
    lon_w = WX_LON_MIN + j * cell_w
    lon_e = lon_w + cell_w
    return lat_n, lat_s, lon_w, lon_e
