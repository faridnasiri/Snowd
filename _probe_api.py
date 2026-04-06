import requests, json

spec = requests.get('https://wcc.sc.egov.usda.gov/awdbRestApi/v3/api-docs', timeout=20).json()

for path, methods in sorted(spec['paths'].items()):
    for verb, det in methods.items():
        print()
        print(verb.upper(), path, '-', det.get('summary',''))
        for p in det.get('parameters', []):
            req = '*' if p.get('required') else ' '
            sch = p.get('schema', {})
            typ = sch.get('type', '')
            enm = sch.get('enum', None)
            name = p.get('name','')
            dsc = (p.get('description') or '')[:100]
            line = '  ['+req+'] '+name.ljust(32)+' '+typ.ljust(10)+' '+dsc
            print(line)
            if enm:
                print('        enum:', enm)
print()

# Also test reference-data endpoint
print('=== /reference-data sample ===')
r2 = requests.get('https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/reference-data', timeout=20)
print('Status:', r2.status_code)
if r2.ok:
    rd = r2.json()
    if isinstance(rd, dict):
        for k, v in list(rd.items())[:5]:
            print(' ', k, ':', str(v)[:120])
    else:
        print(json.dumps(rd, indent=2)[:400])

# Test forecasts endpoint
print()
print('=== /forecasts sample (Stevens Pass, SWE) ===')
r3 = requests.get(
    'https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/forecasts',
    params={'stationTriplets': '791:WA:SNTL', 'elements': 'WTEQ', 'forecastPeriod': '2026-04'},
    timeout=20
)
print('Status:', r3.status_code)
if r3.ok:
    print(json.dumps(r3.json(), indent=2)[:600])
else:
    print(r3.text[:300])

# Test SWE (snow water equivalent) bulk data – more useful than depth for forecasting
print()
print('=== SNWD + WTEQ + TMAX bulk (2025, 3 stations) ===')
r4 = requests.get(
    'https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data',
    params={
        'stationTriplets': '791:WA:SNTL,897:WA:SNTL,899:WA:SNTL',
        'elements': 'SNWD,WTEQ,TMAX,TMIN',
        'beginDate': '2025-04-01',
        'endDate': '2025-04-07',
        'duration': 'DAILY'
    },
    timeout=20
)
print('Status:', r4.status_code)
if r4.ok:
    data = r4.json()
    for rec in data:
        print(' Station:', rec.get('stationTriplet'))
        for elem in rec.get('data', []):
            cd = elem.get('stationElement', {}).get('elementCode')
            vals = elem.get('values', [])[:3]
            print('   ', cd, '->', [(v['date'], v.get('value')) for v in vals])
else:
    print(r4.text[:300])
