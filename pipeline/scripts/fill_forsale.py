import json, re
from pathlib import Path
# Strict same-property matching (see addr_match.py). The old rule matched any
# shared word, including the suburb, so details were copied between
# unrelated properties in the same suburb.
from addr_match import AddressIndex

_DL = Path(__file__).resolve().parent.parent

def norm(a):
    return re.sub(r'[^a-z0-9]', ' ', (a or '').lower()).split()

def street_words(a):
    tokens = norm(a)
    return [t for t in tokens if len(t) > 2 and not t.isdigit()]

h = open(_DL / 'mazar_martin_app.html').read()
m = re.search('"sampleListings"\\s*:\\s*(\\[.*?\\])\\s*[,}]', h, re.DOTALL)
app = json.loads(m.group(1))
domain = json.load(open(_DL / 'domain_forsale_lns.json'))

filled = 0
domain_index = AddressIndex(domain)
for p in app:
    if p.get('propertyType') and p.get('baths') and p.get('parking'):
        continue
    best_match = domain_index.find(p.get('address', ''), p.get('suburb', ''))
    if best_match:
        if not p.get('propertyType') and best_match.get('propertyType'): p['propertyType'] = best_match['propertyType']
        if not p.get('baths') and best_match.get('baths'): p['baths'] = best_match['baths']
        if not p.get('parking') and best_match.get('parking'): p['parking'] = best_match['parking']
        if not p.get('landSize') and best_match.get('landSize'): p['landSize'] = best_match['landSize']
        filled += 1

open(_DL / 'mazar_martin_app.html','w').write(h[:m.start(1)] + json.dumps(app) + h[m.end(1):])
print(f'Filled: {filled}')
