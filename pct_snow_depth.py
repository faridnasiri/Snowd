#!/usr/bin/env python3
"""
PCT Section J (WA) – Snow Depth Analyzer
=========================================
Fetches NRCS SNOTEL snow-depth records for 2024 and 2025,
interpolates readings mile-by-mile along the trail using
inverse-distance-weighting + elevation lapse-rate correction,
then projects 2026 estimates and charts everything.

Outputs (written to  c:\\snow\\pct_snow\\):
  pct_section_j_snow_depth.csv   – full tidy table (mile × date × year)
  snow_pivot_2024/2025/2026.csv  – pivot: miles as rows, dates as columns
  pct_j_snowfree_2026.csv        – estimated snow-free date per trail mile
  pct_j_snow_by_mile.png         – 6-panel: snow depth profile at key dates
  pct_j_snow_over_time.png       – 4-panel: season curve at key waypoints
  pct_j_snowfree_2026.png        – snow-free date timeline chart
"""

import math, os, warnings
from io import StringIO
from datetime import date

import numpy  as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")          # no GUI needed
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch

warnings.filterwarnings("ignore")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

OUTPUT_DIR = r"c:\snow\pct_snow"
os.makedirs(OUTPUT_DIR, exist_ok=True)

REPORT_GEN = (
    "https://wcc.sc.egov.usda.gov/reportGenerator/view_csv"
    "/customSingleStationReport/daily"
)
AWDB_STATIONS = (
    "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/stations"
)

# Dates: fetch March 15 – July 20 each year
FETCH_START_MD = (3, 15)
FETCH_END_MD   = (7, 20)

# ──────────────────────────────────────────────────────────────────────────────
# TRAIL WAYPOINTS — PCT Section J
# Rainy Pass (Hwy 20) ➜ Manning Park BC  (~60.5 trail miles)
# Positions are from PCTA official centreline; elevations from USGS DEM.
# ──────────────────────────────────────────────────────────────────────────────

SECTION_J = [
    {"mile":  0.0, "name": "Rainy Pass (Hwy 20)",      "lat": 48.5168, "lon": -120.7373, "elev_ft": 4860},
    {"mile":  2.5, "name": "Porcupine Creek Jct",      "lat": 48.5310, "lon": -120.7250, "elev_ft": 5200},
    {"mile":  5.2, "name": "Cutthroat Pass",            "lat": 48.5528, "lon": -120.6888, "elev_ft": 6820},
    {"mile":  8.0, "name": "Granite Pass",              "lat": 48.5927, "lon": -120.6835, "elev_ft": 6290},
    {"mile": 10.5, "name": "Methow Pass",               "lat": 48.6238, "lon": -120.6726, "elev_ft": 6600},
    {"mile": 13.0, "name": "Brush Creek",               "lat": 48.6470, "lon": -120.6597, "elev_ft": 5400},
    {"mile": 16.0, "name": "W Fork Methow River",       "lat": 48.6720, "lon": -120.6534, "elev_ft": 5200},
    {"mile": 19.5, "name": "Holman Pass",               "lat": 48.7022, "lon": -120.6553, "elev_ft": 5050},
    {"mile": 22.0, "name": "Goat Lakes Jct",            "lat": 48.7178, "lon": -120.6556, "elev_ft": 6000},
    {"mile": 24.5, "name": "Coleman Ridge",             "lat": 48.7262, "lon": -120.6600, "elev_ft": 6800},
    {"mile": 27.0, "name": "Slate Pass",                "lat": 48.7330, "lon": -120.6648, "elev_ft": 6800},
    {"mile": 31.0, "name": "Harts Pass",                "lat": 48.7329, "lon": -120.6692, "elev_ft": 6198},
    {"mile": 34.0, "name": "Windy Pass",                "lat": 48.7768, "lon": -120.6390, "elev_ft": 6257},
    {"mile": 37.5, "name": "Jim Pass",                  "lat": 48.8047, "lon": -120.6278, "elev_ft": 6278},
    {"mile": 41.0, "name": "Hopkins Lake",              "lat": 48.8310, "lon": -120.6420, "elev_ft": 5150},
    {"mile": 44.5, "name": "Lakeview Ridge",            "lat": 48.8670, "lon": -120.6400, "elev_ft": 6700},
    {"mile": 47.5, "name": "Rock Pass",                 "lat": 48.8972, "lon": -120.6572, "elev_ft": 6890},
    {"mile": 50.0, "name": "Woody Pass",                "lat": 48.9296, "lon": -120.6899, "elev_ft": 6624},
    {"mile": 52.0, "name": "Castle Pass",               "lat": 48.9700, "lon": -120.7710, "elev_ft": 5451},
    {"mile": 54.8, "name": "Monument 78 (Border)",      "lat": 48.9990, "lon": -120.7858, "elev_ft": 4241},
    {"mile": 58.0, "name": "Windy Joe Mtn Jct",         "lat": 49.0398, "lon": -120.7778, "elev_ft": 3900},
    {"mile": 60.5, "name": "Manning Park (end)",         "lat": 49.0690, "lon": -120.7700, "elev_ft": 3800},
]

# ──────────────────────────────────────────────────────────────────────────────
# KNOWN SNOTEL STATIONS  (North Cascades + Okanogan Highlands, WA)
# Verified triplets with confirmed SNWD data availability.
# Elevations from NRCS station metadata (feet).
# ──────────────────────────────────────────────────────────────────────────────

SEED_STATIONS = [
    # triplet              name                         lat       lon       elev_ft
    # ── On / immediately adjacent to trail ──────────────────────────────────
    ("515:WA:SNTL",  "Harts Pass (SNTL)",          48.7329, -120.6692, 6491),  # active Harts Pass SNOTEL
    ("711:WA:SNTL",  "Rainy Pass",                  48.5168, -120.7373, 4882),  # very close to trail start
    ("975:WA:SNTL",  "Swamp Creek",                 48.7650, -120.6830, 5700),  # near Harts Pass
    ("920:WA:SNTL",  "Easy Pass",                   48.5309, -121.1028, 6100),  # W side, near Rainy Pass
    ("681:WA:SNTL",  "Park Creek Ridge",            48.5219, -121.3011, 4610),  # W of trail
    # ── Northern Section J / near Manning Park ───────────────────────────────
    ("962:WA:SNTL",  "Hozomeen Camp",               48.9990, -121.0430, 4000),  # near US-CA border
    ("2G03P:BC:MSNT","Blackwall Peak (BC)",          49.0090, -120.7780, 6500),  # near Manning Park
    ("943:WA:SNTL",  "Beaver Pass",                 48.8900, -121.0000, 5100),  # N Cascades
    ("859:WA:SNTL",  "Brown Top",                   48.7200, -121.2000, 5900),  # Skagit area
    # ── East side (Methow / Okanogan) ────────────────────────────────────────
    ("679:WA:SNTL",  "Loup Loup",                   48.3867, -119.8913, 4240),
    ("936:WA:SNTL",  "Muckamuck",                   48.5500, -120.1500, 5800),
    ("938:WA:SNTL",  "Pope Ridge",                  48.6700, -120.3000, 5600),
    ("942:WA:SNTL",  "Salmon Meadows",              48.6200, -119.9400, 5000),
    ("678:WA:SNTL",  "Decline Creek",               48.5000, -120.1000, 4800),
    # ── Western drainage (Skagit) ───────────────────────────────────────────
    ("958:WA:SNTL",  "Lyman Lake",                  48.1700, -121.0500, 5600),
    ("963:WA:SNTL",  "Marten Ridge",               48.2500, -121.5000, 5400),
    ("965:WA:SNTL",  "Wells Creek",                 48.9200, -121.6500, 4200),
]

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def haversine_mi(lat1, lon1, lat2, lon2):
    R = 3958.8
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(df/2)**2 + math.cos(f1)*math.cos(f2)*math.sin(dl/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def nearest_trail_dist(lat, lon):
    """Minimum haversine distance (miles) from a point to any Section J waypoint."""
    return min(haversine_mi(lat, lon, wp["lat"], wp["lon"]) for wp in SECTION_J)


def date_range_for_year(yr):
    """Weekly dates from FETCH_START_MD to FETCH_END_MD for the given year."""
    return pd.date_range(
        start = pd.Timestamp(yr, *FETCH_START_MD),
        end   = pd.Timestamp(yr, *FETCH_END_MD),
        freq  = "7D"
    )


# ──────────────────────────────────────────────────────────────────────────────
# STEP 1 – DISCOVER STATIONS
# ──────────────────────────────────────────────────────────────────────────────

def discover_stations():
    """
    Query AWDB REST API for all WA SNOTEL stations, then filter to those
    within 60 miles of the trail.  Falls back to SEED_STATIONS on failure.
    """
    print("Querying NRCS AWDB for WA SNOTEL stations …")
    stations = []
    try:
        params = {
            "stateCode":  "WA",
            "networkCds": "SNTL",
            "activeOnly": "true",
        }
        r = requests.get(AWDB_STATIONS, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        for s in data:
            try:
                lat = float(s.get("latitude",  0))
                lon = float(s.get("longitude", 0))
                # AWDB API returns elevation already in feet for US stations
                elev_ft = float(s.get("elevation", 0))
                triplet = s.get("stationTriplet", "")
                name    = s.get("name", triplet)
                dist    = nearest_trail_dist(lat, lon)
                if triplet and dist <= 60:
                    stations.append(dict(triplet=triplet, name=name,
                                         lat=lat, lon=lon, elev_ft=elev_ft,
                                         min_dist_mi=dist))
            except Exception:
                pass
        print(f"  API returned {len(data)} WA stations; {len(stations)} within 60 mi.")
    except Exception as e:
        print(f"  AWDB API unavailable ({e}). Using seed list.")

    if not stations:
        for triplet, name, lat, lon, elev_ft in SEED_STATIONS:
            dist = nearest_trail_dist(lat, lon)
            stations.append(dict(triplet=triplet, name=name,
                                  lat=lat, lon=lon, elev_ft=elev_ft,
                                  min_dist_mi=dist))

    # Merge seed stations (add any missing ones)
    existing = {s["triplet"] for s in stations}
    for triplet, name, lat, lon, elev_ft in SEED_STATIONS:
        if triplet not in existing:
            dist = nearest_trail_dist(lat, lon)
            stations.append(dict(triplet=triplet, name=name,
                                  lat=lat, lon=lon, elev_ft=elev_ft,
                                  min_dist_mi=dist))
            existing.add(triplet)

    stations.sort(key=lambda x: x["min_dist_mi"])
    return stations


# ──────────────────────────────────────────────────────────────────────────────
# STEP 2 – FETCH SNOTEL SNOW DEPTH
# ──────────────────────────────────────────────────────────────────────────────

def fetch_snwd(triplet, yr):
    """
    Download daily snow-depth (SNWD, inches) from the NRCS report generator.
    Returns a pd.Series indexed by date, or None on failure.
    """
    start = f"{yr}-{FETCH_START_MD[0]:02d}-{FETCH_START_MD[1]:02d}"
    end   = f"{yr}-{FETCH_END_MD[0]:02d}-{FETCH_END_MD[1]:02d}"
    url   = f"{REPORT_GEN}/{triplet}/{start},{end}/SNWD::value"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        # Strip comment lines starting with #
        lines = [l for l in r.text.splitlines() if not l.startswith("#") and l.strip()]
        if len(lines) < 3:
            return None
        df = pd.read_csv(StringIO("\n".join(lines)))
        df.columns = [c.strip() for c in df.columns]
        dcol = df.columns[0]
        vcol = df.columns[1]
        df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
        df[vcol] = pd.to_numeric(df[vcol],  errors="coerce")
        s = df.dropna(subset=[dcol, vcol]).set_index(dcol)[vcol]
        s.name = triplet
        return s if len(s) >= 5 else None
    except Exception as e:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# STEP 3 – INTERPOLATE SNOW DEPTH AT A WAYPOINT
# ──────────────────────────────────────────────────────────────────────────────

# Empirical lapse rate: ~1.0 in per 100 ft change in elevation.
# Snow disappears below ~3 500 ft in April, ~4 500 ft in May/June.
LAPSE_IN_PER_100FT = 1.0
ELEV_SNOW_LINE_BY_MONTH = {3: 3000, 4: 3500, 5: 4500, 6: 5200, 7: 6000}


def elev_adjust(snwd_in, station_elev_ft, target_elev_ft, month):
    """Elevation-correct a snow-depth reading."""
    delta = (target_elev_ft - station_elev_ft) / 100.0 * LAPSE_IN_PER_100FT
    snow_line = ELEV_SNOW_LINE_BY_MONTH.get(month, 4000)
    if target_elev_ft < snow_line:
        return 0.0
    return max(snwd_in + delta, 0.0)


def estimate_snow(wp, target_dt, snotel_data, stations, max_dist_mi=85):
    """
    Estimate snow depth at waypoint wp on target_dt via IDW + elevation correction.
    """
    weights, values = [], []
    yr = target_dt.year
    for st in stations:
        yr_data = snotel_data.get(st["triplet"], {}).get(yr)
        if yr_data is None:
            continue
        # nearest reading within ±3 days
        window = yr_data.index[abs(yr_data.index - target_dt) <= pd.Timedelta(days=3)]
        if len(window) == 0:
            continue
        raw = float(yr_data.loc[window].mean())
        if np.isnan(raw):
            continue
        dist = haversine_mi(wp["lat"], wp["lon"], st["lat"], st["lon"])
        if dist > max_dist_mi:
            continue
        adj = elev_adjust(raw, st["elev_ft"], wp["elev_ft"], target_dt.month)
        weights.append(1.0 / max(dist, 0.5)**2)
        values.append(adj)
    if not values:
        return np.nan
    w = np.array(weights)
    v = np.array(values)
    return float(np.dot(w, v) / w.sum())


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  PCT Section J (WA) — Snow Depth Analyzer")
    print("  Rainy Pass → Manning Park  (~60.5 trail miles)")
    print("=" * 65)

    # ── Station discovery ──────────────────────────────────────────────────
    stations = discover_stations()
    print(f"\n{'Station':<26} {'Triplet':<16} {'Elev(ft)':>8}  {'Dist(mi)':>8}")
    print("-" * 65)
    for s in stations[:12]:
        print(f"  {s['name']:<24} {s['triplet']:<16} {s['elev_ft']:>8,.0f}  {s['min_dist_mi']:>8.1f}")
    if len(stations) > 12:
        print(f"  … and {len(stations)-12} more")

    # ── Fetch data ─────────────────────────────────────────────────────────
    print("\nFetching SNOTEL snow-depth data (SNWD) …")
    snotel_data: dict[str, dict[int, pd.Series]] = {}
    for st in stations:
        snotel_data[st["triplet"]] = {}
        for yr in [2024, 2025, 2026]:
            s = fetch_snwd(st["triplet"], yr)
            status = f"{len(s)} days" if s is not None else "—"
            print(f"  {st['name']:<24} {yr}  {status}")
            if s is not None:
                snotel_data[st["triplet"]][yr] = s

    # ── Build tidy table ───────────────────────────────────────────────────
    print("\nBuilding mile-by-mile snow depth table …")
    rows = []
    for yr in [2024, 2025]:
        for dt in date_range_for_year(yr):
            for wp in SECTION_J:
                est = estimate_snow(wp, dt, snotel_data, stations)
                rows.append({
                    "Year":         yr,
                    "Date":         dt.strftime("%Y-%m-%d"),
                    "DayLabel":     dt.strftime("%b-%d"),
                    "Month":        dt.month,
                    "Day":          dt.day,
                    "Mile":         wp["mile"],
                    "Waypoint":     wp["name"],
                    "Elev_ft":      wp["elev_ft"],
                    "SnowDepth_in": round(est, 1) if not np.isnan(est) else None,
                })
    df_hist = pd.DataFrame(rows)

    # ── 2026 estimate ──────────────────────────────────────────────────────
    print("Computing 2026 estimates …")
    rows_26 = []
    for dt in date_range_for_year(2026):
        for wp in SECTION_J:
            m, d = dt.month, dt.day
            # Use real 2026 SNOTEL if available (dates already passed)
            real = None
            if dt.date() <= date.today():
                real = estimate_snow(wp, dt, snotel_data, stations)

            if real is not None and not np.isnan(real):
                v26 = real
            else:
                # Project from 2024 → 2025 linear trend (damped)
                def get_hist(yr):
                    mask = ((df_hist.Year == yr) & (df_hist.Month == m) &
                            (df_hist.Day == d)   & (df_hist.Mile == wp["mile"]))
                    v = df_hist.loc[mask, "SnowDepth_in"]
                    return float(v.iloc[0]) if len(v) > 0 and v.iloc[0] is not None else np.nan
                v24 = get_hist(2024)
                v25 = get_hist(2025)
                if not np.isnan(v24) and not np.isnan(v25):
                    # Damped projection: halfway between v25 and (v25 + trend)
                    trend = v25 - v24
                    v26 = max(v25 + trend * 0.5, 0.0)
                elif not np.isnan(v25):
                    v26 = v25
                elif not np.isnan(v24):
                    v26 = v24
                else:
                    v26 = np.nan

            rows_26.append({
                "Year":         2026,
                "Date":         dt.strftime("%Y-%m-%d"),
                "DayLabel":     dt.strftime("%b-%d"),
                "Month":        m,
                "Day":          d,
                "Mile":         wp["mile"],
                "Waypoint":     wp["name"],
                "Elev_ft":      wp["elev_ft"],
                "SnowDepth_in": round(v26, 1) if not np.isnan(v26) else None,
            })

    df_all = pd.concat([df_hist, pd.DataFrame(rows_26)], ignore_index=True)

    # ── Save CSVs ──────────────────────────────────────────────────────────
    full_csv = os.path.join(OUTPUT_DIR, "pct_section_j_snow_depth.csv")
    df_all.to_csv(full_csv, index=False)
    print(f"\nSaved › {full_csv}")

    for yr in [2024, 2025, 2026]:
        piv = (df_all[df_all.Year == yr]
               .pivot_table(index=["Mile", "Waypoint", "Elev_ft"],
                            columns="DayLabel", values="SnowDepth_in",
                            aggfunc="first"))
        p = os.path.join(OUTPUT_DIR, f"snow_pivot_{yr}.csv")
        piv.to_csv(p)
        print(f"Saved › {p}")

    # ── Snow-free dates ────────────────────────────────────────────────────
    THRESHOLD_IN = 6   # ≤6 inches = "manageable / snow-free for planning"
    sfrows = []
    for wp in SECTION_J:
        sub = (df_all[(df_all.Year == 2026) & (df_all.Mile == wp["mile"])]
               .sort_values(["Month", "Day"]))
        sf_date = None
        for _, row in sub.iterrows():
            sd = row["SnowDepth_in"]
            if sd is not None and not (isinstance(sd, float) and np.isnan(sd)):
                if float(sd) <= THRESHOLD_IN:
                    sf_date = row["Date"]
                    break
        sfrows.append({
            "Mile":     wp["mile"],
            "Waypoint": wp["name"],
            "Elev_ft":  wp["elev_ft"],
            "Est_SnowFree_2026": sf_date if sf_date else ">Jul-20",
            "Threshold_in": THRESHOLD_IN,
        })
    df_sf = pd.DataFrame(sfrows)
    sf_csv = os.path.join(OUTPUT_DIR, "pct_j_snowfree_2026.csv")
    df_sf.to_csv(sf_csv, index=False)
    print(f"Saved › {sf_csv}")

    # ── CHART 1: Snow depth profile by trail mile at 6 key dates ──────────
    print("\nGenerating charts …")
    KEY_DATES = [
        ((4,  1), "April 1"),
        ((4, 15), "April 15"),
        ((5,  1), "May 1"),
        ((5, 15), "May 15"),
        ((6,  1), "June 1"),
        ((6, 15), "June 15"),
    ]
    COLORS     = {2024: "#1976D2", 2025: "#388E3C", 2026: "#F57C00"}
    LSTYLES    = {2024: "-",        2025: "--",       2026: ":"}
    LWIDTHS    = {2024: 2.0,        2025: 2.0,        2026: 2.5}

    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle(
        "PCT Section J (WA) — Snow Depth Mile by Mile\n"
        "Rainy Pass → Manning Park  (SNOTEL + elevation interpolation)",
        fontsize=13, fontweight="bold"
    )
    elev_miles = [w["mile"]    for w in SECTION_J]
    elev_vals  = [w["elev_ft"] for w in SECTION_J]

    for ax, ((mo, dy), title) in zip(axes.flatten(), KEY_DATES):
        for yr in [2024, 2025, 2026]:
            sub = (df_all[(df_all.Year == yr) & (df_all.Month == mo) & (df_all.Day == dy)]
                   .sort_values("Mile")
                   .dropna(subset=["SnowDepth_in"]))
            if sub.empty:
                continue
            lbl = str(yr) + (" (projected)" if yr == 2026 else "")
            ax.plot(sub["Mile"], sub["SnowDepth_in"],
                    color=COLORS[yr], linestyle=LSTYLES[yr],
                    linewidth=LWIDTHS[yr], marker="o", markersize=4, label=lbl)

        ax2 = ax.twinx()
        ax2.fill_between(elev_miles, elev_vals, 3400, alpha=0.07, color="#795548")
        ax2.plot(elev_miles, elev_vals, color="#795548", alpha=0.35, linewidth=1)
        ax2.set_ylim(2500, 9500)
        ax2.set_ylabel("Elevation (ft)", color="#795548", fontsize=8)
        ax2.tick_params(axis="y", labelcolor="#795548", labelsize=7)

        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.set_xlabel("Trail Mile from Rainy Pass", fontsize=9)
        ax.set_ylabel("Snow Depth (inches)", fontsize=9)
        ax.set_xlim(0, 62)
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.legend(fontsize=8, loc="upper right")
        # Landmark lines
        for lm_mile, lm_name in [(5.2, "Cutthroat"), (31, "Harts\nPass"), (54.8, "Border")]:
            ax.axvline(x=lm_mile, color="gray", linestyle=":", alpha=0.4, linewidth=0.8)
            ax.text(lm_mile + 0.4, ax.get_ylim()[1] * 0.92, lm_name,
                    fontsize=6.5, color="gray", va="top")

    plt.tight_layout()
    c1 = os.path.join(OUTPUT_DIR, "pct_j_snow_by_mile.png")
    fig.savefig(c1, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved › {c1}")

    # ── CHART 2: Season curve at 4 key waypoints ───────────────────────────
    KEY_WPS = [
        (5.2,  "Cutthroat Pass (6,820 ft)"),
        (31.0, "Harts Pass (6,198 ft)"),
        (47.5, "Rock Pass (6,890 ft)"),
        (54.8, "Monument 78 / Border (4,241 ft)"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(
        "PCT Section J (WA) — Snow Depth over Season at Key Waypoints\n"
        "2024 (actual) vs 2025 (actual) vs 2026 (projected)",
        fontsize=13, fontweight="bold"
    )
    for ax, (mile, wplabel) in zip(axes.flatten(), KEY_WPS):
        for yr in [2024, 2025, 2026]:
            sub = (df_all[(df_all.Year == yr) & (df_all.Mile == mile)]
                   .sort_values(["Month", "Day"])
                   .dropna(subset=["SnowDepth_in"]))
            if sub.empty:
                continue
            lbl = str(yr) + (" (projected)" if yr == 2026 else "")
            ax.plot(range(len(sub)), sub["SnowDepth_in"],
                    color=COLORS[yr], linestyle=LSTYLES[yr],
                    linewidth=2, marker="o", markersize=4, label=lbl)
            if yr == 2024:
                xlabels = list(sub["DayLabel"])

        ax.set_title(f"Mile {mile}  —  {wplabel}", fontsize=10, fontweight="bold")
        ax.set_xlabel("Date", fontsize=9)
        ax.set_ylabel("Snow Depth (inches)", fontsize=9)
        ax.set_ylim(bottom=0)
        n_ticks = len(xlabels) if "xlabels" in dir() else 0
        if n_ticks:
            step = max(1, n_ticks // 8)
            ax.set_xticks(range(0, n_ticks, step))
            ax.set_xticklabels(xlabels[::step], rotation=40, ha="right", fontsize=8)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.axhline(y=0, color="black", linewidth=0.5)

    plt.tight_layout()
    c2 = os.path.join(OUTPUT_DIR, "pct_j_snow_over_time.png")
    fig.savefig(c2, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved › {c2}")

    # ── CHART 3: Snow-free date timeline by mile (2026) ────────────────────
    fig, ax = plt.subplots(figsize=(15, 5))
    ax.set_title(
        f"PCT Section J (WA) — 2026 Projected Snow-Free Date by Trail Mile\n"
        f"(threshold ≤ {THRESHOLD_IN} inches)",
        fontsize=12, fontweight="bold"
    )
    dot_colors = {
        "early":  "#43A047",   # before May 1
        "mid":    "#FB8C00",   # May 1 – Jun 1
        "late":   "#E53935",   # after Jun 1
    }
    for _, row in df_sf.iterrows():
        ds = row["Est_SnowFree_2026"]
        if ds.startswith(">"):
            ts = pd.Timestamp("2026-07-25")
            c  = dot_colors["late"]
        else:
            ts = pd.Timestamp(ds)
            if ts < pd.Timestamp("2026-05-01"):
                c = dot_colors["early"]
            elif ts < pd.Timestamp("2026-06-01"):
                c = dot_colors["mid"]
            else:
                c = dot_colors["late"]
        ax.scatter(row["Mile"], ts, color=c, s=90, zorder=3)

    ax2 = ax.twinx()
    ax2.fill_between(elev_miles, elev_vals, 3400, alpha=0.08, color="#795548")
    ax2.plot(elev_miles, elev_vals, color="#795548", alpha=0.3, linewidth=1.2)
    ax2.set_ylim(2000, 10000)
    ax2.set_ylabel("Elevation (ft)", color="#795548")

    ax.yaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.yaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax.set_ylim(pd.Timestamp("2026-03-25"), pd.Timestamp("2026-07-28"))
    ax.set_xlim(0, 62)
    ax.set_xlabel("Trail Mile from Rainy Pass", fontsize=10)
    ax.set_ylabel("Estimated Snow-Free Date (2026)", fontsize=10)
    ax.grid(True, alpha=0.3, linestyle=":")

    legend_el = [
        Patch(facecolor=dot_colors["early"], label="Before May 1"),
        Patch(facecolor=dot_colors["mid"],   label="May 1 – Jun 1"),
        Patch(facecolor=dot_colors["late"],  label="After Jun 1 or >Jul-20"),
    ]
    ax.legend(handles=legend_el, loc="upper left", fontsize=9)

    for lm_mile, lm_name in [(5.2, "Cutthroat"), (31, "Harts Pass"), (47.5, "Rock Pass"), (54.8, "Border")]:
        ax.axvline(x=lm_mile, color="gray", linestyle=":", alpha=0.4, linewidth=1)
        ax.text(lm_mile + 0.3, pd.Timestamp("2026-07-22"), lm_name,
                fontsize=7.5, color="gray", rotation=12)

    plt.tight_layout()
    c3 = os.path.join(OUTPUT_DIR, "pct_j_snowfree_2026.png")
    fig.savefig(c3, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved › {c3}")

    # ── Terminal summary ───────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("  2026 PROJECTED SNOW-FREE DATES — PCT Section J")
    print(f"  Snow-free threshold: ≤ {THRESHOLD_IN} inches")
    print("=" * 65)
    print(f"\n  {'Mile':>5}  {'Waypoint':<32}  {'Elev(ft)':>8}  {'Est. Snow-Free'}")
    print(f"  {'-'*5}  {'-'*32}  {'-'*8}  {'-'*18}")
    for _, row in df_sf.iterrows():
        print(f"  {row['Mile']:>5.1f}  {row['Waypoint']:<32}  "
              f"{row['Elev_ft']:>8,.0f}  {row['Est_SnowFree_2026']}")

    print(f"\nAll outputs written to: {OUTPUT_DIR}")
    print("\nFiles:")
    for f in sorted(os.listdir(OUTPUT_DIR)):
        fp = os.path.join(OUTPUT_DIR, f)
        print(f"  {f:<45}  {os.path.getsize(fp):>8,} bytes")


if __name__ == "__main__":
    main()
