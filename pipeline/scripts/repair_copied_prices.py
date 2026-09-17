#!/usr/bin/env python3
"""
repair_copied_prices.py — one-off repair of sold/guide prices copied between
unrelated properties (Sep 2026).

fill_sold_prices.py used to call two addresses the same property when they
shared any word of 3+ letters, with no suburb check. A sold listing whose
price was withheld therefore took the price of an unrelated sale: by Sep 2026,
$800,000 had been written onto 223 sold properties and "Auction Guide
$11,000,000" onto 588. The matcher is fixed (addr_match.py); this script
removes the damage already baked into index.html.

Evidence comes from git: the first committed data (ca9f1f5, 10 Apr 2026)
shows each property's price before any fill ran. For properties present then,
a price filled in since is reverted unless the same property (strict match)
genuinely has that price in the Proping data. For sold listings added later,
a price identical to a Proping figure from a different property is cleared.
The next pipeline run re-fills the genuine ones through the fixed matcher.

Manual sold-price edits live in Supabase and are applied in the app on top of
this data, so they are unaffected.

Usage:
  python repair_copied_prices.py              # dry run: report only
  python repair_copied_prices.py --apply      # rewrite index.html
"""

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from addr_match import AddressIndex  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / 'index.html'
BASELINE_COMMIT = 'ca9f1f5'
WITHHELD = ('', 'price withheld')


def _array_span(html, pattern):
    m = re.search(pattern, html)
    if not m:
        raise SystemExit(f'not found: {pattern}')
    start = m.end() - 1
    value, end = json.JSONDecoder().raw_decode(html, start)
    return value, start, end


def _addr(l):
    return re.sub(r'[^a-z0-9]', '', (l.get('address') or '').lower())


def main():
    apply = '--apply' in sys.argv
    html = INDEX.read_text(encoding='utf-8')
    base_html = subprocess.run(['git', '-C', str(REPO), 'show', f'{BASELINE_COMMIT}:index.html'],
                               capture_output=True, check=True).stdout.decode('utf-8')

    sold, s0, s1 = _array_span(html, r'"soldListings"\s*:\s*\[')
    for_sale, _, _ = _array_span(html, r'"sampleListings"\s*:\s*\[')
    history, _, _ = _array_span(html, r'const propingHistory\s*=\s*\[')
    base_sold, _, _ = _array_span(base_html, r'"soldListings"\s*:\s*\[')
    base = {_addr(l): l for l in base_sold}

    proping = [{'address': e.get('address', ''), 'suburb': e.get('suburb', ''), 'price': e.get('price', '')}
               for d in history for e in d.get('sold', [])
               if e.get('address') and e.get('price') and str(e.get('price')).lower() not in WITHHELD]
    proping_idx = AddressIndex(proping)
    proping_prices = {p['price'] for p in proping}
    sale_idx = AddressIndex([l for l in for_sale if l.get('guidePrice') or l.get('price')])
    guide_counts = Counter(str(l.get('guidePrice') or '') for l in sold if l.get('guidePrice'))

    price_reverted, price_cleared, price_kept = [], [], 0
    guide_reverted, guide_cleared = Counter(), Counter()

    for l in sold:
        b = base.get(_addr(l))
        sp = str(l.get('soldPrice') or '')
        gp = str(l.get('guidePrice') or '')

        # ── sold price ──
        if sp.lower() not in WITHHELD:
            real = proping_idx.find(l.get('address', ''), l.get('suburb', ''))
            genuine = bool(real and real.get('price') == sp)
            # Only a Proping figure can have come from the copy bug; a price from
            # anywhere else (e.g. Domain's own sold data) is left alone.
            copied_signature = sp in proping_prices and not genuine
            if b is not None and str(b.get('soldPrice') or '').lower() in WITHHELD:
                if genuine or not copied_signature:
                    price_kept += 1
                else:
                    price_reverted.append((l.get('address'), sp, b.get('soldPrice') or 'Price Withheld'))
                    l['soldPrice'] = b.get('soldPrice') or 'Price Withheld'
            elif b is None and copied_signature:
                price_cleared.append((l.get('address'), sp))
                l['soldPrice'] = 'Price Withheld'

        # ── guide price ──
        if gp:
            same_sale = sale_idx.find(l.get('address', ''), l.get('suburb', ''))
            genuine = bool(same_sale and gp in (str(same_sale.get('guidePrice') or ''), str(same_sale.get('price') or '')))
            # Copied guides repeat across many unrelated properties; a one-off
            # or pair is more likely a real guide and is left alone.
            if guide_counts[gp] < 3:
                continue
            if b is not None and not str(b.get('guidePrice') or '') and not genuine:
                guide_reverted[gp] += 1
                l['guidePrice'] = ''
            elif b is None and not genuine:
                guide_cleared[gp] += 1
                l['guidePrice'] = ''

    print(f'sold listings: {len(sold)} (baseline {len(base_sold)} from {BASELINE_COMMIT})')
    print(f'sold price reverted to its April value: {len(price_reverted)}')
    print('   most common wrong prices removed:', Counter(p for _, p, _ in price_reverted).most_common(5))
    print(f'sold price cleared on later listings  : {len(price_cleared)}',
          Counter(p for _, p in price_cleared).most_common(3))
    print(f'filled price kept (genuine or non-Proping source): {price_kept}')
    print(f'guide price reverted                  : {sum(guide_reverted.values())}', guide_reverted.most_common(3))
    print(f'guide price cleared on later listings : {sum(guide_cleared.values())}', guide_cleared.most_common(3))
    for a, p, back in price_reverted[:5]:
        print(f'   e.g. {a}: {p} -> {back}')

    if not apply:
        print('\nDRY RUN — nothing written. Re-run with --apply.')
        return
    new_html = html[:s0] + json.dumps(sold) + html[s1:]
    INDEX.write_text(new_html, encoding='utf-8', newline='')
    print(f'\nWrote {INDEX}')


if __name__ == '__main__':
    main()
