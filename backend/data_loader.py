"""Load the hackathon data bundle: flights, sectors, weather strips.

Pickle-caches the expensive STRtree + sector visit data per snapshot.
"""
from __future__ import annotations

import gzip
import json
import logging
import pickle
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from shapely.geometry import shape
from shapely.strtree import STRtree

from .config import CAPACITY_SCALE, DATA_BUNDLE_DIR, SIM_CACHE_DIR
from .schemas import DisruptionPreset, SnapshotInfo

log = logging.getLogger(__name__)


# ── Domain objects ──────────────────────────────────────────────────────────────

@dataclass
class Flight:
    flight_id: str
    flight_number: str
    origin: str
    dest: str
    t0: datetime
    t1: datetime
    cruise_alt_ft: int
    cruise_speed_kt: int
    band: str                   # "HIGH" | "LOW"
    lats: np.ndarray
    lons: np.ndarray
    is_airborne: bool

    def clone(self) -> "Flight":
        return Flight(
            flight_id=self.flight_id,
            flight_number=self.flight_number,
            origin=self.origin,
            dest=self.dest,
            t0=self.t0,
            t1=self.t1,
            cruise_alt_ft=self.cruise_alt_ft,
            cruise_speed_kt=self.cruise_speed_kt,
            band=self.band,
            lats=self.lats.copy(),
            lons=self.lons.copy(),
            is_airborne=self.is_airborne,
        )


@dataclass
class Sector:
    name: str
    band: str                   # "HIGH" | "LOW"
    alt_from_ft: int
    alt_to_ft: int
    capacity: int               # already scaled
    geom: object                # shapely Polygon


@dataclass
class WxStrip:
    based_at: datetime
    valid_from: datetime
    valid_to: datetime
    refc_path: Path
    retop_path: Path
    _refc: np.ndarray | None = field(default=None, repr=False)
    _retop: np.ndarray | None = field(default=None, repr=False)

    def refc(self) -> np.ndarray:
        if self._refc is None:
            self._refc = np.load(self.refc_path)["matrix"]
        return self._refc

    def retop(self) -> np.ndarray:
        if self._retop is None:
            self._retop = np.load(self.retop_path)["matrix"]
        return self._retop


# ── Prebuilt disruption presets ─────────────────────────────────────────────────

PRESETS: list[DisruptionPreset] = [
    DisruptionPreset(
        id="midwest_storm",
        label="Convective line over the Midwest",
        kind="STORM",
        description="Threshold refc ≥ 40 dBZ; close impacted HIGH sectors.",
    ),
    DisruptionPreset(
        id="ord_ground_stop",
        label="ORD ground stop",
        kind="GROUND_STOP",
        description="Hold KORD departures 45 min.",
    ),
    DisruptionPreset(
        id="northeast_closure",
        label="Northeast sector closure",
        kind="SECTOR_CLOSURE",
        description="Close a cluster of HIGH sectors in the Northeast.",
    ),
]


# ── Snapshot discovery ──────────────────────────────────────────────────────────

_DT_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_snapshot_id(dirname: str) -> str | None:
    """Extract the snapshot timestamp string from a directory name."""
    m = re.match(r"asked_at_(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", dirname)
    return m.group(1) if m else None


def list_snapshots(bundle_dir: Path = DATA_BUNDLE_DIR) -> list[SnapshotInfo]:
    infos: list[SnapshotInfo] = []
    for d in sorted(bundle_dir.iterdir()):
        sid = _parse_snapshot_id(d.name)
        if sid is None or not d.is_dir():
            continue
        routes_path = d / "routes.json"
        if not routes_path.exists():
            routes_path = d / "routes.json.gz"
        if not routes_path.exists():
            continue
        try:
            raw = _load_json(routes_path)
            infos.append(SnapshotInfo(
                id=sid,
                asked_at=raw["asked_at"],
                window_start=raw["window_start"],
                window_end=raw["window_end"],
                flight_count=len(raw["flights"]),
            ))
        except Exception as e:
            log.warning("Skipping snapshot %s: %s", sid, e)
    return infos


# ── JSON loading (handles plain + gzip) ─────────────────────────────────────────

def _load_json(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Flight parsing ──────────────────────────────────────────────────────────────

def _parse_datetime(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_flights(snapshot_id: str, bundle_dir: Path = DATA_BUNDLE_DIR) -> tuple[dict[str, Flight], str, str]:
    """Return (flights_dict, window_start_iso, window_end_iso)."""
    snap_dir = bundle_dir / f"asked_at_{snapshot_id}"
    routes_path = snap_dir / "routes.json"
    if not routes_path.exists():
        routes_path = snap_dir / "routes.json.gz"

    raw = _load_json(routes_path)
    flights: dict[str, Flight] = {}
    for rec in raw["flights"]:
        fn = rec["flight_number"]
        t0 = _parse_datetime(rec["take_off_time"])
        t1 = _parse_datetime(rec["scheduled_landing_time"])
        origin = rec["origin_airport_icao"]
        dest = rec["destination_airport_icao"]
        flight_id = f"{fn}|{t0.isoformat()}|{origin}"
        band = "HIGH" if rec["cruise_altitude_ft"] >= 35000 else "LOW"
        flights[flight_id] = Flight(
            flight_id=flight_id,
            flight_number=fn,
            origin=origin,
            dest=dest,
            t0=t0,
            t1=t1,
            cruise_alt_ft=rec["cruise_altitude_ft"],
            cruise_speed_kt=rec["cruise_speed_kt"],
            band=band,
            lats=np.array(rec["lats"], dtype=np.float64),
            lons=np.array(rec["lons"], dtype=np.float64),
            is_airborne=rec["is_airborne"],
        )
    return flights, raw["window_start"], raw["window_end"]


# ── Sector loading ──────────────────────────────────────────────────────────────

def load_sectors(bundle_dir: Path = DATA_BUNDLE_DIR) -> dict[str, Sector]:
    geojson_path = bundle_dir / "sectors.geojson"
    if not geojson_path.exists():
        geojson_path = bundle_dir / "sectors.geojson.gz"

    raw = _load_json(geojson_path)
    sectors: dict[str, Sector] = {}
    for feat in raw["features"]:
        props = feat["properties"]
        name: str = props["name"]
        band = "HIGH" if name.startswith("HIGH") else "LOW"
        geom = shape(feat["geometry"])
        raw_cap = int(props["capacity"])
        scaled_cap = max(1, round(raw_cap * CAPACITY_SCALE))
        sectors[name] = Sector(
            name=name,
            band=band,
            alt_from_ft=int(props["altitude_from_ft"]),
            alt_to_ft=int(props["altitude_to_ft"]),
            capacity=scaled_cap,
            geom=geom,
        )
    log.info("Loaded %d sectors (capacity_scale=%.2f)", len(sectors), CAPACITY_SCALE)
    return sectors


def build_strtrees(sectors: dict[str, Sector]) -> dict[str, tuple[STRtree, list[str]]]:
    """Return per-band (STRtree, [sector_name_at_index]) for spatial queries."""
    result: dict[str, tuple[STRtree, list[str]]] = {}
    for band in ("HIGH", "LOW"):
        band_sectors = [(n, s) for n, s in sectors.items() if s.band == band]
        geoms = [s.geom for _, s in band_sectors]
        names = [n for n, _ in band_sectors]
        result[band] = (STRtree(geoms), names)
    return result


# ── Weather strip loading ───────────────────────────────────────────────────────

_WX_FNAME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})_(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})_(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})\.npz"
)
_WX_DT_FMT = "%Y-%m-%d_%H:%M:%S"


def _parse_wx_dt(s: str) -> datetime:
    return datetime.strptime(s, _WX_DT_FMT).replace(tzinfo=timezone.utc)


def load_wx_strips(snapshot_id: str, bundle_dir: Path = DATA_BUNDLE_DIR) -> list[WxStrip]:
    snap_dir = bundle_dir / f"asked_at_{snapshot_id}"
    refc_dir = snap_dir / "wx" / "refc"
    retop_dir = snap_dir / "wx" / "retop"

    if not refc_dir.exists():
        log.warning("No wx data found for snapshot %s", snapshot_id)
        return []

    strips: list[WxStrip] = []
    for refc_file in sorted(refc_dir.iterdir()):
        m = _WX_FNAME_RE.match(refc_file.name)
        if not m:
            continue
        based_at = _parse_wx_dt(m.group(1))
        valid_from = _parse_wx_dt(m.group(2))
        valid_to = _parse_wx_dt(m.group(3))
        retop_file = retop_dir / refc_file.name
        if not retop_file.exists():
            continue
        strips.append(WxStrip(
            based_at=based_at,
            valid_from=valid_from,
            valid_to=valid_to,
            refc_path=refc_file,
            retop_path=retop_file,
        ))
    log.info("Loaded %d wx strips for snapshot %s", len(strips), snapshot_id)
    return strips


# ── Pickle cache ────────────────────────────────────────────────────────────────

def cache_path(snapshot_id: str) -> Path:
    SIM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = snapshot_id.replace(":", "-")
    return SIM_CACHE_DIR / f"{safe}.pkl"


def save_cache(snapshot_id: str, data: object) -> None:
    with open(cache_path(snapshot_id), "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_cache(snapshot_id: str) -> object | None:
    p = cache_path(snapshot_id)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        log.warning("Cache load failed for %s: %s", snapshot_id, e)
        return None
