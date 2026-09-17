import json, re
from pathlib import Path
# Strict same-property matching (see addr_match.py). The old rule matched any
# shared word, including the suburb, so details were copied between
# unrelated properties in the same suburb.
from addr_match import AddressIndex

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
fs_index = AddressIndex(domain_fs)
sold_index = AddressIndex(domain_sold)

for day in history:
    for p in day.get('newly_listed', []):
        # Skip only if EVERY fillable field is already present. Since Proping's
        # 8 Sep 2026 layout change the email itself supplies baths/car/land,
        # so propertyType is now usually the only gap — it must be part of
        # the skip test or Domain would never fill it in.
        if p.get('baths') and p.get('heroPhoto') and p.get('propertyType'): continue
        d = fs_index.find(p.get('address', ''), p.get('suburb', ''))
        if d:
            # Fill blanks only: values parsed from the Proping email win.
            for f in ('baths', 'parking', 'propertyType', 'landSize'):
                if d.get(f) and not p.get(f): p[f] = d[f]
            if d.get('heroPhoto') and not p.get('heroPhoto'):
                p['heroPhoto'] = d['heroPhoto']
                filled_photo += 1
            filled_listed += 1

    for p in day.get('sold', []):
        if p.get('baths') and p.get('heroPhoto') and p.get('propertyType'): continue
        d = sold_index.find(p.get('address', ''), p.get('suburb', ''))
        if d:
            for f in ('baths', 'parking', 'propertyType', 'landSize'):
                if d.get(f) and not p.get(f): p[f] = d[f]
            if d.get('method'): p['method'] = d['method']
            if d.get('heroPhoto') and not p.get('heroPhoto'):
                p['heroPhoto'] = d['heroPhoto']
                filled_photo += 1
            filled_sold += 1

APP_PATH.write_text(html[:m.start(1)] + json.dumps(history) + html[m.end(1):], encoding="utf-8")
print(f'Newly listed filled: {filled_listed} | Sold filled: {filled_sold} | Photos filled: {filled_photo}')
