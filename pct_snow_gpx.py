#!/usr/bin/env python3
"""
PCT Section J (Snoqualmie Pass → Stevens Pass, WA) – GPX-Based Snow Analyzer
=============================================================================
Uses the actual Garmin GPX track  (COURSE_334291582.gpx)  to extract
real GPS coordinates and elevations at every trail mile, then fetches
NRCS SNOTEL snow-depth data for 2024, 2025, and 2026.

Mile-by-mile snow depth is estimated via inverse-distance-weighting of
nearby SNOTEL stations, corrected for elevation using a lapse-rate model.
2026 is projected from the 2024–2025 trend where real data is unavailable.

Outputs (c:\\snow\\pct_snow\\):
  gpx_waypoints.csv               – mile-by-mile waypoints from your GPX
  pct_section_j_snow_depth.csv    – full tidy table (mile × date × year)
  snow_pivot_2024/2025/2026.csv   – pivot tables
  pct_j_snowfree_2026.csv         – estimated snow-free date per mile
  pct_j_snow_by_mile.png          – 6-panel profile chart
  pct_j_snow_over_time.png        – season melt curves at key waypoints
  pct_j_snowfree_2026.png         – timeline of snow-free dates by mile
"""

import math, os, warnings
import xml.etree.ElementTree as ET
from datetime import date

import numpy  as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch

warnings.filterwarnings("ignore")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

GPX_FILE   = r"c:\repos\snowd\COURSE_334291582.gpx"
OUTPUT_DIR = r"c:\repos\snowd\pct_snow"
os.makedirs(OUTPUT_DIR, exist_ok=True)

AWDB_STATIONS  = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/stations"
AWDB_DATA      = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data"
OPEN_METEO_FC  = "https://api.open-meteo.com/v1/forecast"
NWS_GRIDPOINT  = "https://api.weather.gov/gridpoints/SEW/151,53"   # Snoqualmie Pass area only (single ~2.5 km cell)
NWS_HEADERS    = {"User-Agent": "PCT-SnowAnalyzer/1.0"}
MAX_STATIONS   = 20   # capped; chunked bulk calls prevent URL-length truncation
CHUNK_SIZE     = 15   # max station triplets per AWDB bulk API call

FETCH_START_MD = (3, 15)
FETCH_END_MD   = (7, 20)

# Daily sampling: eliminates the ±3.5-day error inherent in 7-day sampling.
SAMPLE_FREQ = "1D"

# Minimum consecutive days below THRESHOLD_IN to declare a mile snow-free.
# Prevents false positives from short warm spells that are followed by refreeze.
CONSECUTIVE_DAYS = 5

# Seasonal lapse rates (inches per 100 ft elevation change).
# Increases through the season as the snowpack becomes more sensitive to elevation.
LAPSE_BY_MONTH = {3: 0.6, 4: 0.8, 5: 1.1, 6: 1.4, 7: 1.6}

# Snow-line base elevations and a±400 ft transition zone.
# A smooth ramp replaces the hard cliff that caused profile discontinuities.
ELEV_SNOW_LINE_BY_MONTH  = {3: 2500, 4: 3000, 5: 3800, 6: 4500, 7: 5200}
SNOW_LINE_TRANSITION_FT  = 400

# ──────────────────────────────────────────────────────────────────────────────
# KNOWN NAMED LANDMARKS along Section J (Snoqualmie → Stevens Pass)
# Used only for labeling; actual positions come from the GPX.
# ──────────────────────────────────────────────────────────────────────────────

LANDMARKS = {
    # approx trail mile : label
     0: "Snoqualmie Pass (Hwy 90)",
     5: "Gold Lake Bog area",
    10: "Chikamin Peak area",
    15: "Park Lakes Basin",
    20: "Waptus River",
    25: "Deep Lake / Cathedral Pass",
    30: "Deception Pass",
    35: "Deception Creek",
    40: "Surprise Lake Jct",
    45: "Trap Pass area",
    50: "Necklace Valley Jct",
    55: "Hope Lake / Mig Lake",
    60: "Cady Pass",
    65: "Lake Valhalla area",
    70: "Stevens Pass (Hwy 2)",
}

# ──────────────────────────────────────────────────────────────────────────────
# SEED SNOTEL STATIONS — Central WA Cascades (Snoqualmie → Stevens zone)
# Verified triplets with consistent SNWD data.
# ──────────────────────────────────────────────────────────────────────────────

SEED_STATIONS = [
    # triplet              name                           lat       lon       elev_ft
    # ── On / very near the trail ──────────────────────────────────────────────
    ("908:WA:SNTL",  "Snoqualmie Pass",               47.4254, -121.4168, 3000),
    ("909:WA:SNTL",  "Stampede Pass",                 47.2820, -121.3380, 3970),
    ("635:WA:SNTL",  "Meadows",                       47.6270, -121.4330, 4500),  # skykomish drainage
    ("764:WA:SNTL",  "Rainy Creek",                   47.7460, -121.0890, 4060),  # near Stevens Pass
    ("1049:WA:SNTL", "Grouse Camp",                   47.5400, -121.2200, 4380),  # Alpine Lakes
    # ── East-side stations (Wenatchee/Blewett drainage) ──────────────────────
    ("618:WA:SNTL",  "Blewett",                       47.3320, -120.5710, 4150),
    ("623:WA:SNTL",  "Tumwater Mountain",             47.5970, -120.7070, 5040),
    ("631:WA:SNTL",  "Chiwaukum Creek",               47.6960, -120.7010, 3400),
    # ── West-side stations (Skykomish drainage) ──────────────────────────────────────────────
    ("775:WA:SNTL",  "Skykomish",                     47.7070, -121.3580, 1140),
    # Wells Creek (48.92°N, Mt. Baker region) and Cayuse Pass (46.87°N, Mt. Rainier)
    # removed — both are >50 miles from the trail corridor and bias IDW estimates.
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


def date_range_for_year(yr):
    return pd.date_range(
        start=pd.Timestamp(yr, *FETCH_START_MD),
        end  =pd.Timestamp(yr, *FETCH_END_MD),
        freq =SAMPLE_FREQ
    )

def api_get(url, params=None, headers=None, timeout=60, retries=3, backoff=2.0):
    """GET with exponential-backoff retries. Raises on final failure."""
    import time
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    raise last_exc

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1 – PARSE GPX → mile-by-mile waypoints
# ──────────────────────────────────────────────────────────────────────────────

def parse_gpx(gpx_path):
    """
    Parse the GPX track and return a list of dicts with keys:
        mile, lat, lon, elev_ft, name
    sampled at every integer trail mile.
    """
    print(f"Parsing GPX: {gpx_path}")
    tree = ET.parse(gpx_path)
    root = tree.getroot()
    ns   = {"g": "http://www.topografix.com/GPX/1/1"}

    raw_pts = []
    for pt in root.findall(".//g:trkpt", ns):
        lat  = float(pt.get("lat"))
        lon  = float(pt.get("lon"))
        ee   = pt.find("g:ele", ns)
        elev = float(ee.text) * 3.28084 if ee is not None else 0.0
        raw_pts.append((lat, lon, elev))

    # Build cumulative distance array
    cum = [0.0]
    for i in range(1, len(raw_pts)):
        d = haversine_mi(raw_pts[i-1][0], raw_pts[i-1][1],
                         raw_pts[i][0],   raw_pts[i][1])
        cum.append(cum[-1] + d)

    total_miles = cum[-1]
    print(f"  {len(raw_pts):,} track points  |  total length: {total_miles:.2f} miles")

    # Sample at every integer mile + endpoints
    sample_miles = [0.0] + list(range(1, int(total_miles))) + [round(total_miles, 2)]
    waypoints = []
    j = 0
    for target in sample_miles:
        # Advance j until we bracket target
        while j < len(cum) - 1 and cum[j+1] < target:
            j += 1
        if j >= len(raw_pts) - 1:
            lat, lon, elev = raw_pts[-1]
        else:
            # Linear interpolation between j and j+1
            span = cum[j+1] - cum[j]
            frac = (target - cum[j]) / span if span > 0 else 0.0
            lat  = raw_pts[j][0] + frac * (raw_pts[j+1][0] - raw_pts[j][0])
            lon  = raw_pts[j][1] + frac * (raw_pts[j+1][1] - raw_pts[j][1])
            elev = raw_pts[j][2] + frac * (raw_pts[j+1][2] - raw_pts[j][2])

        mile_int = int(round(target))
        name = LANDMARKS.get(mile_int, f"Mile {target:.1f}")
        waypoints.append({
            "mile":    round(target, 1),
            "name":    name,
            "lat":     round(lat,  6),
            "lon":     round(lon,  6),
            "elev_ft": round(elev, 0),
        })

    print(f"  Extracted {len(waypoints)} mile-markers")
    print(f"  Start: {waypoints[0]['name']}  ({waypoints[0]['lat']}, {waypoints[0]['lon']})  {waypoints[0]['elev_ft']:.0f} ft")
    print(f"  End:   {waypoints[-1]['name']}  ({waypoints[-1]['lat']}, {waypoints[-1]['lon']})  {waypoints[-1]['elev_ft']:.0f} ft")
    return waypoints


# ──────────────────────────────────────────────────────────────────────────────
# STEP 2 – DISCOVER SNOTEL STATIONS
# ──────────────────────────────────────────────────────────────────────────────

def discover_stations(waypoints):
    """Query AWDB API for WA SNOTEL stations; filter to those within 70 miles."""
    print("\nQuerying NRCS AWDB for WA SNOTEL stations …")

    def nearest_dist(lat, lon):
        return min(haversine_mi(lat, lon, w["lat"], w["lon"]) for w in waypoints)

    stations = []
    # Try API first
    for state in ["WA", "OR"]:
        try:
            params = {"stateCode": state, "networkCds": "SNTL", "activeOnly": "true"}
            r = api_get(AWDB_STATIONS, params=params, timeout=60)
            for s in r.json():
                try:
                    lat     = float(s.get("latitude",  0))
                    lon     = float(s.get("longitude", 0))
                    elev_ft = float(s.get("elevation", 0))
                    triplet = s.get("stationTriplet", "")
                    name    = s.get("name", triplet)
                    dist    = nearest_dist(lat, lon)
                    if triplet and dist <= 70:
                        stations.append(dict(triplet=triplet, name=name,
                                              lat=lat, lon=lon, elev_ft=elev_ft,
                                              min_dist_mi=dist))
                except Exception:
                    pass
        except Exception as e:
            print(f"  AWDB API unavailable for {state} ({e}). Using seed list.")

    if not stations:
        print("  Falling back to seed station list.")
        for triplet, name, lat, lon, elev_ft in SEED_STATIONS:
            dist = nearest_dist(lat, lon)
            stations.append(dict(triplet=triplet, name=name,
                                  lat=lat, lon=lon, elev_ft=elev_ft,
                                  min_dist_mi=dist))
    else:
        print(f"  Found {len(stations)} stations within 70 mi of the trail.")
        # Merge seed stations to ensure key ones are present
        existing = {s["triplet"] for s in stations}
        for triplet, name, lat, lon, elev_ft in SEED_STATIONS:
            if triplet not in existing:
                dist = nearest_dist(lat, lon)
                stations.append(dict(triplet=triplet, name=name,
                                      lat=lat, lon=lon, elev_ft=elev_ft,
                                      min_dist_mi=dist))

    stations.sort(key=lambda x: x["min_dist_mi"])
    # De-duplicate by triplet (API sometimes returns dupes), keep closest
    seen = {}
    for s in stations:
        if s["triplet"] not in seen:
            seen[s["triplet"]] = s
    stations = list(seen.values())[:MAX_STATIONS]
    print(f"  Using {len(stations)} closest unique stations.")
    return stations


# ──────────────────────────────────────────────────────────────────────────────
# STEP 3 – FETCH SNOTEL SNOW DEPTH  (bulk: one API call per year)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_snwd_chunked(stations, yr):
    """
    Fetch daily SNWD + WTEQ + TMAX + TMIN for all stations, split into
    batches of CHUNK_SIZE to prevent URL-length truncation in the AWDB REST API.
    Returns dict: triplet -> {"SNWD": pd.Series, "WTEQ": pd.Series, ...}
    """
    start  = f"{yr}-{FETCH_START_MD[0]:02d}-{FETCH_START_MD[1]:02d}"
    end    = f"{yr}-{FETCH_END_MD[0]:02d}-{FETCH_END_MD[1]:02d}"
    result = {}
    chunks = [stations[i:i+CHUNK_SIZE] for i in range(0, len(stations), CHUNK_SIZE)]
    for chunk in chunks:
        triples = ",".join(s["triplet"] for s in chunk)
        params  = {
            "stationTriplets": triples,
            "elements":        "SNWD,WTEQ,TMAX,TMIN",
            "beginDate":       start,
            "endDate":         end,
            "duration":        "DAILY",
        }
        try:
            r = api_get(AWDB_DATA, params=params, timeout=90)
            for rec in r.json():
                triplet = rec.get("stationTriplet", "")
                station_elem = {}
                for elem in rec.get("data", []):
                    code = elem["stationElement"]["elementCode"]
                    vals = elem.get("values", [])
                    if not vals:
                        continue
                    dates  = pd.to_datetime([v["date"] for v in vals], errors="coerce")
                    values = pd.to_numeric([v.get("value") for v in vals], errors="coerce")
                    s = pd.Series(list(values), index=dates).dropna()
                    if len(s) >= 5:
                        station_elem[code] = s
                if station_elem:
                    result[triplet] = station_elem
        except Exception as e:
            print(f"  Chunk AWDB fetch error for {yr} (chunk of {len(chunk)} stations): {e}")
    return result


# STEP 3b (REMOVED): Open-Meteo ERA5 archive calls removed.
# ERA5 ~9 km grid cells underestimate high-elevation snowpack, and the fetched
# values were never used in estimate_snow() — pure wasted network time.


# ──────────────────────────────────────────────────────────────────────────────
# STEP 3c – NWS: current snow level (snow-line elevation) + 16-day temp forecast
# ──────────────────────────────────────────────────────────────────────────────

def fetch_nws_snow_level():
    """
    Returns list of {validTime, snow_level_ft} for the next ~7 days.
    NOTE: NWS_GRIDPOINT is a single ~2.5 km cell near Snoqualmie Pass;
    snow-line values are indicative for the southern trail end only.
    """
    try:
        r = api_get(NWS_GRIDPOINT, headers=NWS_HEADERS, timeout=20)
        gp = r.json()["properties"]
        entries = []
        for v in gp.get("snowLevel", {}).get("values", []):
            val_m = v.get("value")
            if val_m is not None:
                entries.append({
                    "validTime":     v["validTime"],
                    "snow_level_ft": round(float(val_m) * 3.28084),
                })
        return entries
    except Exception as e:
        print(f"  NWS snowLevel fetch error: {e}")
        return []


def fetch_open_meteo_forecast(lat, lon, days=16):
    """16-day daily snow forecast from Open-Meteo for a specific lat/lon."""
    try:
        r = api_get(OPEN_METEO_FC, params={
            "latitude":  lat, "longitude": lon,
            "daily":     "snowfall_sum,snow_depth_max,temperature_2m_max,temperature_2m_min",
            "timezone":  "America/Los_Angeles",
            "forecast_days": days,
        }, timeout=20)
        return r.json().get("daily", {})
    except Exception:
        pass
    return {}


# ──────────────────────────────────────────────────────────────────────────────
# STEP 4 – SNOW DEPTH INTERPOLATION
# ──────────────────────────────────────────────────────────────────────────────

# Seasonal lapse rates override the old fixed LAPSE_IN_PER_100FT constant.
# Old ELEV_SNOW_LINE_BY_MONTH hard-cliff replaced by snow_line_factor() below.


def snow_line_factor(elev_ft, month):
    """Smooth 0→1 ramp across ±SNOW_LINE_TRANSITION_FT around the monthly snow line.
    Eliminates the hard cliff that produced step discontinuities in snow profiles."""
    sl = ELEV_SNOW_LINE_BY_MONTH.get(month, 3500)
    lo = sl - SNOW_LINE_TRANSITION_FT
    hi = sl + SNOW_LINE_TRANSITION_FT
    if elev_ft >= hi:
        return 1.0
    if elev_ft <= lo:
        return 0.0
    return (elev_ft - lo) / (hi - lo)


def elev_adjust(snwd_in, station_elev_ft, target_elev_ft, month):
    lapse  = LAPSE_BY_MONTH.get(month, 1.0)
    delta  = (target_elev_ft - station_elev_ft) / 100.0 * lapse
    factor = snow_line_factor(target_elev_ft, month)
    return max((snwd_in + delta) * factor, 0.0)


def estimate_snow(wp, target_dt, snotel_data, stations, element="SNWD", max_dist_mi=60):
    weights, values = [], []
    yr = target_dt.year
    for st in stations:
        st_elems = snotel_data.get(st["triplet"], {}).get(yr, {})
        yr_data  = st_elems.get(element) if isinstance(st_elems, dict) else st_elems
        if yr_data is None:
            continue
        mask = abs(yr_data.index - target_dt) <= pd.Timedelta(days=3)
        if not mask.any():
            continue
        raw = float(yr_data[mask].mean())
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
    print("=" * 68)
    print("  PCT Section J (WA) — GPX-Based Snow Depth Analyzer")
    print("  Snoqualmie Pass (Hwy 90) → Stevens Pass (Hwy 2)")
    print("=" * 68)

    # ── 1. Parse GPX ───────────────────────────────────────────────────────
    waypoints = parse_gpx(GPX_FILE)

    # Save waypoints CSV
    wp_csv = os.path.join(OUTPUT_DIR, "gpx_waypoints.csv")
    pd.DataFrame(waypoints).to_csv(wp_csv, index=False)
    print(f"Saved waypoints › {wp_csv}")

    # ── 2. Discover stations ───────────────────────────────────────────────
    stations = discover_stations(waypoints)
    print(f"\n{'Station':<26} {'Triplet':<16} {'Elev(ft)':>8}  {'Dist(mi)':>8}")
    print("-" * 68)
    for s in stations[:15]:
        print(f"  {s['name']:<24} {s['triplet']:<16} {s['elev_ft']:>8,.0f}  {s['min_dist_mi']:>8.1f}")
    if len(stations) > 15:
        print(f"  … and {len(stations)-15} more")

    # ── 3. Fetch SNOTEL data (3 bulk calls total) ─────────────────────────
    print("\nFetching SNOTEL SNWD + WTEQ + TMAX/TMIN — chunked bulk API calls per year …")
    snotel_data: dict = {st["triplet"]: {} for st in stations}
    for yr in [2024, 2025, 2026]:
        yr_data = fetch_snwd_chunked(stations, yr)
        count = sum(1 for v in yr_data.values() if "SNWD" in v)
        print(f"  {yr}: {count} stations with SNWD  ({len(yr_data)} total records)")
        for triplet, elems in yr_data.items():
            if triplet in snotel_data:
                snotel_data[triplet][yr] = elems

    fetched_stations = [st for st in stations
                        if any(v is not None for v in snotel_data[st["triplet"]].values())]
    print(f"  {len(fetched_stations)} stations with usable data:")
    for st in fetched_stations:
        yrs_str = []
        for yr in [2024, 2025, 2026]:
            elems = snotel_data[st["triplet"]].get(yr, {})
            if isinstance(elems, dict):
                codes = list(elems.keys())
                n = len(next(iter(elems.values()), []))
                yrs_str.append(f"{yr}:{n}d({','.join(codes)})")
            else:
                yrs_str.append(f"{yr}:—")
        print(f"    {st['name']:<26} {'  '.join(yrs_str)}")

    if not fetched_stations:
        print("ERROR: No SNOTEL data returned. Check network.")
        return

    # ── 3c. NWS current snow level ──────────────────────────────────────────
    print("\nFetching NWS snow level (snow-line elevation) …")
    nws_snow_level = fetch_nws_snow_level()
    if nws_snow_level:
        current_sl = nws_snow_level[0]["snow_level_ft"]
        print(f"  Current snow line: {current_sl:,} ft  (at {nws_snow_level[0]['validTime'][:16]})"
              f"  [Snoqualmie Pass area; single NWS grid cell]")
        upcoming = nws_snow_level[1:8]
        for e in upcoming:
            print(f"    {e['validTime'][:16]}  {e['snow_level_ft']:,} ft")
    else:
        current_sl = None
        print("  NWS snow level unavailable")

    # ── 3d. Open-Meteo 16-day forecast at Stevens Pass ─────────────────────
    print("\nFetching Open-Meteo 16-day forecast at Stevens Pass …")
    fc_WP = waypoints[-1]  # Stevens Pass end
    om_fc = fetch_open_meteo_forecast(fc_WP["lat"], fc_WP["lon"])
    if om_fc:
        fc_times  = om_fc.get("time", [])
        fc_snow   = om_fc.get("snowfall_sum", [])
        fc_depth  = om_fc.get("snow_depth_max", [])
        fc_tmax   = om_fc.get("temperature_2m_max", [])
        print(f"  {'Date':<12} {'Snowfall(cm)':>12} {'Depth(cm)':>10} {'Tmax(°C)':>9}")
        for i in range(min(16, len(fc_times))):
            print(f"  {fc_times[i]:<12} {fc_snow[i] if fc_snow else '':>12} "
                  f"{fc_depth[i] if fc_depth else '':>10} "
                  f"{fc_tmax[i] if fc_tmax else '':>9}")
    else:
        print("  Open-Meteo forecast unavailable")

    # ── 4. Build tidy table  2024 + 2025 ──────────────────────────────────
    print("\nBuilding mile-by-mile snow depth table …")
    rows = []
    for yr in [2024, 2025]:
        for dt in date_range_for_year(yr):
            for wp in waypoints:
                est_snwd = estimate_snow(wp, dt, snotel_data, fetched_stations, element="SNWD")
                est_wteq = estimate_snow(wp, dt, snotel_data, fetched_stations, element="WTEQ")
                rows.append({
                    "Year":        yr,
                    "Date":        dt.strftime("%Y-%m-%d"),
                    "DayLabel":    dt.strftime("%b-%d"),
                    "Month":       dt.month,
                    "Day":         dt.day,
                    "Mile":        wp["mile"],
                    "Waypoint":    wp["name"],
                    "Lat":         wp["lat"],
                    "Lon":         wp["lon"],
                    "Elev_ft":     wp["elev_ft"],
                    "SnowDepth_in": round(est_snwd, 1) if not np.isnan(est_snwd) else None,
                    "SWE_in":       round(est_wteq, 1) if not np.isnan(est_wteq) else None,
                })
    df_hist = pd.DataFrame(rows)

    # Build fast lookup index for 2026 projection: (year, month, day, mile) -> (snwd, swe)
    # Avoids O(n²) DataFrame mask scans when date_range is daily.
    hist_index = {}
    for _, row in df_hist.iterrows():
        key = (int(row["Year"]), int(row["Month"]), int(row["Day"]), row["Mile"])
        hist_index[key] = (row["SnowDepth_in"], row["SWE_in"])

    def _lookup(yr, m, d, mile, col_idx):
        v = hist_index.get((yr, m, d, mile), (np.nan, np.nan))[col_idx]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return np.nan
        return float(v)

    # ── 5. 2026 projection ─────────────────────────────────────────────────
    print("Computing 2026 estimates …")
    rows_26 = []
    for dt in date_range_for_year(2026):
        for wp in waypoints:
            m, d = dt.month, dt.day

            # Use real 2026 SNOTEL data up to today; project beyond that.
            real = None
            if dt.date() <= date.today():
                real = estimate_snow(wp, dt, snotel_data, fetched_stations)

            if real is not None and not np.isnan(real):
                v26 = real
            else:
                v24 = _lookup(2024, m, d, wp["mile"], 0)
                v25 = _lookup(2025, m, d, wp["mile"], 0)
                if not np.isnan(v24) and not np.isnan(v25):
                    # Weighted average (65% recent year / 35% prior year).
                    # No trend extrapolation — two years is insufficient.
                    v26 = max(0.65 * v25 + 0.35 * v24, 0.0)
                elif not np.isnan(v25):
                    v26 = v25
                elif not np.isnan(v24):
                    v26 = v24
                else:
                    v26 = np.nan

            # WTEQ projection (same logic)
            real_wteq = None
            if dt.date() <= date.today():
                real_wteq = estimate_snow(wp, dt, snotel_data, fetched_stations, element="WTEQ")
            if real_wteq is not None and not np.isnan(real_wteq):
                w26 = real_wteq
            else:
                w24 = _lookup(2024, m, d, wp["mile"], 1)
                w25 = _lookup(2025, m, d, wp["mile"], 1)
                if not np.isnan(w24) and not np.isnan(w25):
                    w26 = max(0.65 * w25 + 0.35 * w24, 0.0)
                elif not np.isnan(w25):
                    w26 = w25
                elif not np.isnan(w24):
                    w26 = w24
                else:
                    w26 = np.nan

            rows_26.append({
                "Year":        2026,
                "Date":        dt.strftime("%Y-%m-%d"),
                "DayLabel":    dt.strftime("%b-%d"),
                "Month":       m,
                "Day":         d,
                "Mile":        wp["mile"],
                "Waypoint":    wp["name"],
                "Lat":         wp["lat"],
                "Lon":         wp["lon"],
                "Elev_ft":     wp["elev_ft"],
                "SnowDepth_in": round(v26, 1) if not np.isnan(v26) else None,
                "SWE_in":       round(w26, 1) if not np.isnan(w26) else None,
            })

    df_all = pd.concat([df_hist, pd.DataFrame(rows_26)], ignore_index=True)

    # ── 6. Save CSVs ───────────────────────────────────────────────────────
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

    # ── 7. Snow-free dates ─────────────────────────────────────────────────
    THRESHOLD_IN = 6
    sfrows = []
    for wp in waypoints:
        sub = (df_all[(df_all.Year == 2026) & (df_all.Mile == wp["mile"])]
               .sort_values(["Month", "Day"])
               .reset_index(drop=True))
        sf_date    = None
        streak     = 0
        streak_start = None
        for _, row in sub.iterrows():
            sd = row["SnowDepth_in"]
            below = (sd is not None
                     and not (isinstance(sd, float) and np.isnan(sd))
                     and float(sd) <= THRESHOLD_IN)
            if below:
                streak += 1
                if streak_start is None:
                    streak_start = row["Date"]
                if streak >= CONSECUTIVE_DAYS:
                    sf_date = streak_start
                    break
            else:
                streak = 0
                streak_start = None
        sfrows.append({
            "Mile":     wp["mile"],
            "Waypoint": wp["name"],
            "Elev_ft":  wp["elev_ft"],
            "Est_SnowFree_2026": sf_date if sf_date else ">Jul-20",
            "Threshold_in": THRESHOLD_IN,
            "Consecutive_days_required": CONSECUTIVE_DAYS,
        })
    df_sf = pd.DataFrame(sfrows)
    sf_csv = os.path.join(OUTPUT_DIR, "pct_j_snowfree_2026.csv")
    df_sf.to_csv(sf_csv, index=False)
    print(f"Saved › {sf_csv}")

    # ── 8. CHART 1 – Profile at 6 key dates ───────────────────────────────
    print("\nGenerating charts …")
    KEY_DATES = [
        ((4,  1), "April 1"),
        ((4, 15), "April 15"),
        ((5,  1), "May 1"),
        ((5, 15), "May 15"),
        ((6,  1), "June 1"),
        ((6, 15), "June 15"),
    ]
    COLORS  = {2024: "#1976D2", 2025: "#388E3C", 2026: "#F57C00"}
    LSTYLES = {2024: "-",        2025: "--",       2026: ":"}
    LWIDTHS = {2024: 2.0,        2025: 2.0,        2026: 2.5}

    elev_miles = [w["mile"]    for w in waypoints]
    elev_vals  = [w["elev_ft"] for w in waypoints]

    fig, axes = plt.subplots(2, 3, figsize=(20, 11))
    fig.suptitle(
        "PCT Section J (WA) — Snow Depth Mile by Mile  [GPX-based]\n"
        "Snoqualmie Pass → Stevens Pass  (SNOTEL + elevation interpolation)",
        fontsize=13, fontweight="bold"
    )

    for ax, ((mo, dy), title) in zip(axes.flatten(), KEY_DATES):
        for yr in [2024, 2025, 2026]:
            target_dt = pd.Timestamp(f"{yr}-{mo:02d}-{dy:02d}")
            yr_rows = df_all[df_all.Year == yr]
            if yr_rows.empty:
                continue
            yr_dates = pd.to_datetime(yr_rows["Date"]).unique()
            nearest = min(yr_dates, key=lambda d: abs((d - target_dt).days))
            sub = (yr_rows[pd.to_datetime(yr_rows["Date"]) == nearest]
                   .sort_values("Mile").dropna(subset=["SnowDepth_in"]))
            if sub.empty:
                continue
            lbl = str(yr) + (" (projected)" if yr == 2026 else "")
            ax.plot(sub["Mile"], sub["SnowDepth_in"],
                    color=COLORS[yr], linestyle=LSTYLES[yr],
                    linewidth=LWIDTHS[yr], marker="o", markersize=3, label=lbl)

        _e_lo = min(elev_vals) - 400
        _e_hi = max(elev_vals) + 800
        ax2 = ax.twinx()
        ax2.fill_between(elev_miles, elev_vals, _e_lo, alpha=0.15, color="#795548")
        ax2.plot(elev_miles, elev_vals, color="#795548", alpha=0.65, linewidth=1.5)
        ax2.set_ylim(_e_lo, _e_hi)
        ax2.set_ylabel("Elevation (ft)", color="#795548", fontsize=8)
        ax2.tick_params(axis="y", labelcolor="#795548", labelsize=7)

        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.set_xlabel("Trail Mile from Snoqualmie Pass", fontsize=9)
        ax.set_ylabel("Snow Depth (inches)", fontsize=9)
        ax.set_xlim(0, max(elev_miles) + 1)
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.legend(fontsize=8, loc="upper right")
        # Mark key landmarks
        for lm_mile, lm_name in [(25, "Cathedral\nPass"), (30, "Deception\nPass"), (70, "Stevens\nPass")]:
            ax.axvline(x=lm_mile, color="gray", linestyle=":", alpha=0.4, linewidth=0.8)
            ax.text(lm_mile + 0.4, 0.88, lm_name,
                    transform=ax.get_xaxis_transform(),
                    fontsize=6.5, color="gray", va="top")

    plt.tight_layout()
    c1 = os.path.join(OUTPUT_DIR, "pct_j_snow_by_mile.png")
    fig.savefig(c1, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved › {c1}")

    # ── 9. CHART 2 – Season curves at 4 key waypoints ─────────────────────
    # Pick waypoints at approx miles 0, 25, 45, 70
    target_miles = [0, 25, 45, 70]
    key_wps = []
    for tm in target_miles:
        closest = min(waypoints, key=lambda w: abs(w["mile"] - tm))
        key_wps.append(closest)

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(
        "PCT Section J (WA) — Snow Depth over Season at Key Waypoints  [GPX-based]\n"
        "2024 (actual) vs 2025 (actual) vs 2026 (projected)",
        fontsize=13, fontweight="bold"
    )

    for ax, wp in zip(axes.flatten(), key_wps):
        for yr in [2024, 2025, 2026]:
            sub = (df_all[(df_all.Year == yr) & (df_all.Mile == wp["mile"])]
                   .sort_values(["Month", "Day"])
                   .dropna(subset=["SnowDepth_in"]))
            if sub.empty:
                continue
            lbl = str(yr) + (" (projected)" if yr == 2026 else "")
            # Map all years onto a single reference year so curves align on the same axis.
            dates = pd.to_datetime(sub["Date"].str.replace(r"^\d{4}", "2000", regex=True))
            ax.plot(dates, sub["SnowDepth_in"],
                    color=COLORS[yr], linestyle=LSTYLES[yr],
                    linewidth=2, marker="o", markersize=2, label=lbl)

        ax.set_title(f"Mile {wp['mile']:.0f}  —  {wp['name']}\n"
                     f"({wp['lat']:.4f}, {wp['lon']:.4f})  {wp['elev_ft']:.0f} ft",
                     fontsize=9, fontweight="bold")
        ax.set_xlabel("Date", fontsize=9)
        ax.set_ylabel("Snow Depth (inches)", fontsize=9)
        ax.set_ylim(bottom=0)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0, interval=2))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=40, ha="right", fontsize=8)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3, linestyle=":")
        ax.axhline(y=0, color="black", linewidth=0.5)

    plt.tight_layout()
    c2 = os.path.join(OUTPUT_DIR, "pct_j_snow_over_time.png")
    fig.savefig(c2, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved › {c2}")

    # ── 10. CHART 3 – Snow-free timeline ──────────────────────────────────
    fig, ax = plt.subplots(figsize=(16, 5))
    ax.set_title(
        f"PCT Section J (WA) — 2026 Projected Snow-Free Date by Trail Mile  [GPX-based]\n"
        f"Snoqualmie Pass → Stevens Pass  (threshold ≤ {THRESHOLD_IN} inches)",
        fontsize=12, fontweight="bold"
    )
    dot_colors = {
        "early":    "#43A047",   # before May 1
        "mid":      "#FB8C00",   # May 1 – Jun 1
        "late":     "#E53935",   # after Jun 1
        "no_clear": "#7B1FA2",   # still ≥ threshold at Jul 20 (distinct from 'late')
    }
    for _, row in df_sf.iterrows():
        ds = row["Est_SnowFree_2026"]
        if ds.startswith(">"):
            ts = pd.Timestamp("2026-07-25")
            c  = dot_colors["no_clear"]
        else:
            ts = pd.Timestamp(ds)
            c  = (dot_colors["early"] if ts < pd.Timestamp("2026-05-01") else
                  dot_colors["mid"]   if ts < pd.Timestamp("2026-06-01") else
                  dot_colors["late"])
        ax.scatter(row["Mile"], ts, color=c, s=70, zorder=3)

    _e_lo = min(elev_vals) - 400
    _e_hi = max(elev_vals) + 800
    ax2 = ax.twinx()
    ax2.fill_between(elev_miles, elev_vals, _e_lo, alpha=0.15, color="#795548")
    ax2.plot(elev_miles, elev_vals, color="#795548", alpha=0.65, linewidth=1.5)
    ax2.set_ylim(_e_lo, _e_hi)
    ax2.set_ylabel("Elevation (ft)", color="#795548")

    ax.yaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.yaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax.set_ylim(pd.Timestamp("2026-03-20"), pd.Timestamp("2026-07-28"))
    ax.set_xlim(-1, max(elev_miles) + 1)
    ax.set_xlabel("Trail Mile from Snoqualmie Pass", fontsize=10)
    ax.set_ylabel("Estimated Snow-Free Date (2026)", fontsize=10)
    ax.grid(True, alpha=0.3, linestyle=":")

    legend_el = [
        Patch(facecolor=dot_colors["early"],    label="Before May 1"),
        Patch(facecolor=dot_colors["mid"],      label="May 1 – Jun 1"),
        Patch(facecolor=dot_colors["late"],     label="After Jun 1"),
        Patch(facecolor=dot_colors["no_clear"], label=f"Not clear by Jul-20 (plotted at Jul-25)"),
    ]
    ax.legend(handles=legend_el, loc="lower right", fontsize=9)

    for lm_mile, lm_name in [(25, "Cathedral Pass"), (30, "Deception Pass"), (70, "Stevens Pass")]:
        if lm_mile <= max(elev_miles):
            ax.axvline(x=lm_mile, color="gray", linestyle=":", alpha=0.5, linewidth=1)
            ax.text(lm_mile + 0.3, pd.Timestamp("2026-07-22"), lm_name,
                    fontsize=7.5, color="gray", rotation=12)

    plt.tight_layout()
    c3 = os.path.join(OUTPUT_DIR, "pct_j_snowfree_2026.png")
    fig.savefig(c3, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved › {c3}")

    # ── 11. Terminal summary ───────────────────────────────────────────────
    # Current conditions from NWS snow level
    if nws_snow_level:
        sl_now = nws_snow_level[0]["snow_level_ft"]
        sl_max3 = max(e["snow_level_ft"] for e in nws_snow_level[:12])
        print("\n" + "=" * 68)
        print("  CURRENT CONDITIONS  (NWS + SNOTEL live readings)")
        print("=" * 68)
        print(f"  Snow line (NWS, now):       {sl_now:>6,} ft")
        print(f"  Snow line (NWS, 3-day max): {sl_max3:>6,} ft")
        # Stevens Pass SNOTEL live
        sp_trip = next((s["triplet"] for s in fetched_stations if "Stevens" in s["name"]), None)
        if sp_trip:
            sp_snwd = snotel_data.get(sp_trip, {}).get(2026, {}).get("SNWD")
            sp_wteq = snotel_data.get(sp_trip, {}).get(2026, {}).get("WTEQ")
            if sp_snwd is not None and len(sp_snwd):
                latest_d = sp_snwd.index[-1].strftime("%Y-%m-%d")
                print(f"  Stevens Pass SNWD (live):  {sp_snwd.iloc[-1]:>6.0f} in  (as of {latest_d})")
            if sp_wteq is not None and len(sp_wteq):
                print(f"  Stevens Pass SWE  (live):  {sp_wteq.iloc[-1]:>6.1f} in")
        if om_fc:
            next_snow = [(om_fc['time'][i], om_fc.get('snowfall_sum', [])[i])
                         for i in range(len(om_fc.get('time', [])))
                         if om_fc.get('snowfall_sum', [None]*99)[i] and
                            float(om_fc['snowfall_sum'][i] or 0) > 0.5]
            if next_snow:
                print(f"  Next snowfall (forecast):  {next_snow[0][0]}  {next_snow[0][1]:.1f} cm")

    print("\n" + "=" * 68)
    print("  2026 PROJECTED SNOW-FREE DATES  —  PCT Section J")
    print(f"  Snoqualmie Pass → Stevens Pass  |  threshold ≤ {THRESHOLD_IN} in SNWD")
    print("=" * 68)
    print(f"\n  {'Mile':>5}  {'Waypoint':<32}  {'Elev(ft)':>8}  {'Est. Snow-Free'}")
    print(f"  {'-'*5}  {'-'*32}  {'-'*8}  {'-'*20}")
    for _, row in df_sf.iterrows():
        print(f"  {row['Mile']:>5.1f}  {row['Waypoint']:<32}  "
              f"{row['Elev_ft']:>8,.0f}  {row['Est_SnowFree_2026']}")

    print(f"\nAll outputs: {OUTPUT_DIR}")
    for f in sorted(os.listdir(OUTPUT_DIR)):
        fp = os.path.join(OUTPUT_DIR, f)
        print(f"  {f:<48}  {os.path.getsize(fp):>8,} bytes")


if __name__ == "__main__":
    main()
