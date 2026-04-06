import requests, json

hdrs = {'User-Agent': 'PCT-SnowAnalyzer/1.0'}

# ── NWS gridpoint snow elements ──────────────────────────────────────────────
print("=" * 60)
print("NWS Gridpoint SEW/151,53 (Snoqualmie Pass area)")
print("=" * 60)
gd = requests.get("https://api.weather.gov/gridpoints/SEW/151,53",
                  headers=hdrs, timeout=20).json()
gp = gd["properties"]

print("\n--- snowfallAmount (next periods, mm converted to inches) ---")
for v in gp["snowfallAmount"].get("values", [])[:10]:
    val_mm = v.get("value", 0) or 0
    print(f"  {v['validTime'][:22]}  {val_mm:.1f} mm = {val_mm/25.4:.1f} in")

print("\n--- snowLevel (snow line elevation) ---")
for v in gp["snowLevel"].get("values", [])[:10]:
    val_m = v.get("value") or 0
    val_ft = round(val_m * 3.28084)
    print(f"  {v['validTime'][:22]}  {val_m:.0f} m = {val_ft} ft")

# ── Open-Meteo Archive — historical gridded snow depth, FREE, no key ─────────
print()
print("=" * 60)
print("Open-Meteo Archive API  (no API key needed)")
print("Snoqualmie Pass lat/lon, 2024 & 2025 melt season")
print("=" * 60)
for yr, start, end in [
    (2024, "2024-03-15", "2024-07-20"),
    (2025, "2025-03-15", "2025-07-20"),
]:
    r = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": 47.4074, "longitude": -121.4301,
            "start_date": start, "end_date": end,
            "daily": "snowfall_sum,snow_depth_max,temperature_2m_max,temperature_2m_min",
            "timezone": "America/Los_Angeles",
        }, timeout=20)
    print(f"\n  Year {yr}: status={r.status_code}")
    if r.ok:
        daily = r.json().get("daily", {})
        times   = daily.get("time", [])
        sdmax   = daily.get("snow_depth_max", [])
        snfall  = daily.get("snowfall_sum", [])
        # print every ~2 weeks
        step = 14
        for i in range(0, len(times), step):
            print(f"    {times[i]}  snow_depth={sdmax[i]}cm  snowfall_sum={snfall[i]}cm")
    else:
        print("  ERROR:", r.text[:200])

# ── Open-Meteo Forecast — includes snow depth forecast ───────────────────────
print()
print("=" * 60)
print("Open-Meteo Forecast API  (7-16 day, no key)")
print("=" * 60)
r = requests.get(
    "https://api.open-meteo.com/v1/forecast",
    params={
        "latitude": 47.4074, "longitude": -121.4301,
        "daily": "snowfall_sum,snow_depth_max,temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "America/Los_Angeles",
        "forecast_days": 16,
    }, timeout=20)
print(f"Status: {r.status_code}")
if r.ok:
    daily = r.json().get("daily", {})
    print("Variables:", list(daily.keys()))
    times   = daily.get("time", [])
    sdmax   = daily.get("snow_depth_max", [])
    snfall  = daily.get("snowfall_sum", [])
    tmax    = daily.get("temperature_2m_max", [])
    for i in range(len(times)):
        print(f"  {times[i]}  snow_depth={sdmax[i]}cm  snowfall={snfall[i]}cm  tmax={tmax[i]}C")
else:
    print("ERROR:", r.text[:200])

# ── AWDB /forecasts endpoint ─────────────────────────────────────────────────
print()
print("=" * 60)
print("AWDB /forecasts endpoint")
print("=" * 60)
r = requests.get(
    "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/forecasts",
    params={
        "stationTriplets": "791:WA:SNTL",
        "elementCd": "WTEQ",
        "forecastPeriod": "JAN",
    }, timeout=20)
print(f"Status: {r.status_code}")
if r.ok:
    print(json.dumps(r.json(), indent=2)[:800])
else:
    print(r.text[:300])
