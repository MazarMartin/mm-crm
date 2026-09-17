import json, re
from pathlib import Path
# Strict same-property matching (see addr_match.py). The previous rule had no
# suburb check and matched any shared word of 3+ letters ("street", "road"),
# so a sold listing with a withheld price could take the price of an unrelated
# property anywhere in the dataset.
from addr_match import AddressIndex

_DL = Path(__file__).resolve().parent.parent

proping = json.load(open(_DL / 'proping_history.json'))
domain_fs = json.load(open(_DL / 'domain_forsale_lns.json'))

# Proping sold rows carry `price` (Proping's figure for the sale).
proping_sold = [
    {'address': s.get('address', ''), 'suburb': s.get('suburb', ''), 'price': s.get('price', '')}
    for day in proping for s in day.get('sold', [])
    if s.get('address') and s.get('price') and 'withheld' not in str(s.get('price')).lower()
]
domain_guide = [d for d in domain_fs if d.get('address') and d.get('price')]
print(f'Proping sold with a price: {len(proping_sold)} | Domain guide prices: {len(domain_guide)}')

proping_index = AddressIndex(proping_sold)
guide_index = AddressIndex(domain_guide)

h = open(_DL / 'mazar_martin_app.html').read()
m = re.search('"soldListings"\\s*:\\s*(\\[.*?\\])\\s*[,}]', h, re.DOTALL)
sold = json.loads(m.group(1))

updated = 0
guides = 0
for s in sold:
    if not s.get('soldPrice') or s.get('soldPrice') == 'Price Withheld':
        p = proping_index.find(s.get('address', ''), s.get('suburb', ''))
        if p:
            s['soldPrice'] = p['price']
            updated += 1
    if not s.get('guidePrice'):
        d = guide_index.find(s.get('address', ''), s.get('suburb', ''))
        if d:
            s['guidePrice'] = d['price']
            guides += 1

print(f'Sold prices filled: {updated} | Guide prices filled: {guides}')
open(_DL / 'mazar_martin_app.html', 'w').write(h[:m.start(1)] + json.dumps(sold) + h[m.end(1):])
print('Done.')
