import json, re
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent.parent / "mazar_martin_app.html"

def street_words(a):
    tokens = re.sub(r'[^a-z0-9]', ' ', (a or '').lower()).split()
    return [t for t in tokens if len(t) > 2 and not t.isdigit()]

def match(a, b, sa, sb):
    suburb_ok = sa and sb and (sa in sb or sb in sa)
    words_ok = len(set(street_words(a)) & set(street_words(b))) >= 1
    return suburb_ok and words_ok

html = APP_PATH.read_text(encoding="utf-8")
m = re.search(r'propingHistory\s*=\s*(\[.*?\])\s*;', html, re.DOTALL)
history = json.loads(m.group(1))

domain_fs = json.load(open(Path(__file__).resolve().parent.parent / 'domain_forsale_lns.json'))
domain_sold = json.load(open(Path(__file__).resolve().parent.parent / 'domain_sold_lns.json'))

# Diagnostic: how much of the Domain scrape actually has heroPhoto URLs.
# Zero here means the scrape is coming back empty for photos — the fix
# then can't fill anything and the swipe deck stays photo-less.
_fs_photo = sum(1 for d in domain_fs if d.get('heroPhoto'))
_sold_photo = sum(1 for d in domain_sold if d.get('heroPhoto'))
print(f'Domain fs items with heroPhoto: {_fs_photo}/{len(domain_fs)} | '
      f'Domain sold items with heroPhoto: {_sold_photo}/{len(domain_sold)}')

filled_listed = 0
filled_sold = 0
filled_photo = 0

for day in history:
    for p in day.get('newly_listed', []):
        # Skip only if EVERY fillable field is already present. Previously
        # this skipped as soon as `baths` was set, which meant heroPhoto
        # (added later) never backfilled onto existing items.
        if p.get('baths') and p.get('heroPhoto'): continue
        pa, ps = p.get('address',''), p.get('suburb','').lower()
        for d in domain_fs:
            if match(pa, d.get('address',''), ps, d.get('suburb','').lower()):
                if d.get('baths'): p['baths'] = d['baths']
                if d.get('parking'): p['parking'] = d['parking']
                if d.get('propertyType'): p['propertyType'] = d['propertyType']
                if d.get('landSize'): p['landSize'] = d['landSize']
                if d.get('heroPhoto') and not p.get('heroPhoto'):
                    p['heroPhoto'] = d['heroPhoto']
                    filled_photo += 1
                filled_listed += 1
                break

    for p in day.get('sold', []):
        if p.get('baths') and p.get('heroPhoto'): continue
        pa, ps = p.get('address',''), p.get('suburb','').lower()
        for d in domain_sold:
            if match(pa, d.get('address',''), ps, d.get('suburb','').lower()):
                if d.get('baths'): p['baths'] = d['baths']
                if d.get('parking'): p['parking'] = d['parking']
                if d.get('propertyType'): p['propertyType'] = d['propertyType']
                if d.get('landSize'): p['landSize'] = d['landSize']
                if d.get('method'): p['method'] = d['method']
                if d.get('heroPhoto') and not p.get('heroPhoto'):
                    p['heroPhoto'] = d['heroPhoto']
                    filled_photo += 1
                filled_sold += 1
                break

APP_PATH.write_text(html[:m.start(1)] + json.dumps(history) + html[m.end(1):], encoding="utf-8")
print(f'Newly listed filled: {filled_listed} | Sold filled: {filled_sold} | Photos filled: {filled_photo}')
