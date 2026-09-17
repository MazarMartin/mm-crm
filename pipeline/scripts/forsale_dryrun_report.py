#!/usr/bin/env python3
"""forsale_dryrun_report.py: READ-ONLY preview of loading Domain's full
For Sale / Sold inventory into the app. Writes nothing.

Answers: how many listings would be added, how complete they are, and how
many listings the app shows today that Domain no longer lists.
Run by .github/workflows/forsale-dryrun.yml (manual only).
"""
import json, os, re, sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from addr_match import address_key, AddressIndex

_DL = Path(__file__).resolve().parent.parent
APP = _DL / 'mazar_martin_app.html'
FS = _DL / 'domain_forsale_lns.json'
SOLD = _DL / 'domain_sold_lns.json'
STATS = _DL / 'domain_scrape_stats.json'


def js_array(html, key):
    m = re.search(r'"%s"\s*:\s*\[' % key, html)
    if not m:
        return []
    i = m.end() - 1
    depth, in_str, esc = 0, False, False
    for j in range(i, len(html)):
        c = html[j]
        if in_str:
            if esc: esc = False
            elif c == '\\': esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == '[': depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return json.loads(html[i:j + 1])
    return []


def has_price(s):
    """Same idea as the app's parsePrice: a dollar figure over 50,000."""
    for n in re.findall(r'\d[\d,]*', str(s or '')):
        if int(n.replace(',', '')) > 50000:
            return True
    return False


def pct(n, d):
    return f'{round(100 * n / d)}%' if d else '-'


def completeness(rows, price_field):
    n = len(rows)
    return {
        'price': pct(sum(any(has_price(r.get(f)) for f in price_field.split('|')) for r in rows), n),
        'beds': pct(sum(bool(str(r.get('beds') or '').strip()) for r in rows), n),
        'baths': pct(sum(bool(str(r.get('baths') or '').strip()) for r in rows), n),
        'type': pct(sum(bool(str(r.get('propertyType') or '').strip()) for r in rows), n),
        'photo': pct(sum(bool(r.get('heroPhoto')) for r in rows), n),
    }


def main():
    out = []
    say = out.append
    for p in (FS, SOLD, APP):
        if not p.exists():
            print(f'Missing {p.name}: nothing to preview')
            return
    fs = json.load(open(FS, encoding='utf-8'))
    sold = json.load(open(SOLD, encoding='utf-8'))
    html = open(APP, encoding='utf-8').read()
    app_fs = js_array(html, 'sampleListings')
    app_sold = js_array(html, 'soldListings')

    say('## For Sale inventory dry run (nothing changed)')
    if STATS.exists():
        st = json.load(open(STATS, encoding='utf-8'))
        say(f"Domain data from the scrape finished {st.get('finished') or st.get('updated') or '?'}")
    say('')

    for label, dom, app, pf_dom, pf_app in (
        ('For Sale', fs, app_fs, 'price', 'guidePrice|price'),
        ('Sold', sold, app_sold, 'soldPrice', 'soldPrice'),
    ):
        dom_keys = {}
        for r in dom:
            k = address_key(r.get('address', ''), r.get('suburb', ''))
            if k[0]:
                dom_keys.setdefault(k, r)
        dom_unique = list(dom_keys.values())
        app_index = AddressIndex(app)
        dom_index = AddressIndex(dom_unique)
        new = [r for r in dom_unique if not app_index.find(r.get('address', ''), r.get('suburb', ''))]
        already = len(dom_unique) - len(new)
        gone = [r for r in app if address_key(r.get('address', ''), r.get('suburb', ''))[0]
                and not dom_index.find(r.get('address', ''), r.get('suburb', ''))]

        say(f'### {label}')
        say('| | Count |')
        say('|---|---|')
        say(f'| Listings in the app today | {len(app)} |')
        say(f'| Listings on Domain (unique addresses) | {len(dom_unique)} |')
        say(f'| Already in the app | {already} |')
        say(f'| **Would be added** | **{len(new)}** |')
        say(f'| In the app but not in Domain\'s current list | {len(gone)} |')
        say('')
        c_new = completeness(new, pf_dom)
        c_app = completeness(app, pf_app)
        say('| Has this detail | New from Domain | App today |')
        say('|---|---|---|')
        for f in ('price', 'beds', 'baths', 'type', 'photo'):
            say(f'| {f} | {c_new[f]} | {c_app[f]} |')
        say('')
        subs = Counter((r.get('suburb') or 'Unknown') for r in new)
        say('Would be added, by suburb: ' + ', '.join(f'{s} {n}' for s, n in subs.most_common(12)))
        say('')

    text = '\n'.join(out)
    print(text)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write(text + '\n')


if __name__ == '__main__':
    main()
