#!/usr/bin/env python3
"""
scrape_domain_realestate.py
Scrapes Domain.com.au (and optionally realestate.com.au) for LNS properties,
using Playwright headless Chromium to bypass bot detection.

Outputs three files in the pipeline/ folder for downstream consumers:

    domain_scraped_data.json   — legacy combined output (inject_offmarket.py)
    domain_forsale_lns.json    — flat list for wash_properties.py (For Sale)
    domain_sold_lns.json       — flat list for wash_properties.py (Sold)

Each listing in the wash-compatible files contains:
    address, suburb, price / soldPrice, soldDate, beds, baths, parking,
    propertyType, landSize, url, agentNames, agencyName

Usage:
    python3 scrape_domain_realestate.py
    python3 scrape_domain_realestate.py --sold-only
    python3 scrape_domain_realestate.py --listed-only
    python3 scrape_domain_realestate.py --pages 3       # pages per suburb
    python3 scrape_domain_realestate.py --suburbs mosman cremorne
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Playwright / Patchright ──────────────────────────────────────────────────
# Prefer Patchright (a maintained fork of Playwright with better anti-bot
# stealth — specifically designed to slip past Akamai/Cloudflare). Falls back
# to vanilla Playwright if Patchright isn't installed, so this stays runnable
# in dev environments that don't have it. Both expose the same sync_api.
_DRIVER = None
try:
    from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _DRIVER = 'patchright'
except ImportError:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        _DRIVER = 'playwright'
    except ImportError:
        print("Installing playwright...")
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'playwright',
                        '--break-system-packages'], check=True)
        subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'],
                       check=True)
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        _DRIVER = 'playwright'


# ── Config ───────────────────────────────────────────────────────────────────
SCRIPT_DIR        = Path(__file__).parent
DOWNLOADS         = SCRIPT_DIR.parent
OUTPUT_LEGACY     = DOWNLOADS / 'domain_scraped_data.json'
OUTPUT_FORSALE    = DOWNLOADS / 'domain_forsale_lns.json'
OUTPUT_SOLD       = DOWNLOADS / 'domain_sold_lns.json'

LNS_SUBURBS = [
    ('mosman',           'NSW', '2088'),
    ('neutral-bay',      'NSW', '2089'),
    ('cremorne',         'NSW', '2090'),
    ('cremorne-point',   'NSW', '2090'),
    ('kirribilli',       'NSW', '2061'),
    ('milsons-point',    'NSW', '2061'),
    ('mcmahons-point',   'NSW', '2060'),
    ('north-sydney',     'NSW', '2060'),
    ('waverton',         'NSW', '2060'),
    ('lavender-bay',     'NSW', '2060'),
    ('wollstonecraft',   'NSW', '2065'),
    ('cammeray',         'NSW', '2062'),
    ('crows-nest',       'NSW', '2065'),
    ('naremburn',        'NSW', '2065'),
    ('kurraba-point',    'NSW', '2089'),
    ('beauty-point',     'NSW', '2088'),
    ('balmoral',         'NSW', '2088'),
    ('willoughby',       'NSW', '2068'),
    ('chatswood',        'NSW', '2067'),
    ('northbridge',      'NSW', '2063'),
    ('castlecrag',       'NSW', '2068'),
    ('castle-cove',      'NSW', '2069'),
    ('middle-cove',      'NSW', '2068'),
    ('hunters-hill',     'NSW', '2110'),
    ('artarmon',         'NSW', '2064'),
]

USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/125.0.0.0 Safari/537.36'
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _norm_type(t):
    if not t:
        return ''
    t = str(t).strip()
    low = re.sub(r'([a-z])([A-Z])', r'\1 \2', t).lower()
    if 'apartment' in low or 'unit' in low or 'flat' in low:
        return 'Apartment'
    if 'townhouse' in low or 'town house' in low:
        return 'Townhouse'
    if 'semi' in low or 'duplex' in low:
        return 'Semi-Detached'
    if 'villa' in low:
        return 'Villa'
    if 'studio' in low:
        return 'Studio'
    if 'land' in low or 'block' in low:
        return 'Land'
    if 'house' in low or 'home' in low:
        return 'House'
    return t.title()


def _walk(obj, fn):
    """Recursively yield values matching fn."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v, fn)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, fn)
    else:
        if fn(obj):
            yield obj


def _find_listings_in_json(blob):
    """
    Extract listing objects from Domain's Next.js blob.
    Primary location: props.pageProps.componentProps.listingsMap (dict keyed by id).
    Each value has {id, listingType, listingModel}. We unwrap listingModel and
    drop entries where listingType == 'project' (those are off-the-plan bundles
    without per-unit features).
    """
    out = []
    # Walk the whole blob looking for 'listingsMap' dicts
    def visit(x, parent_key=None):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == 'listingsMap' and isinstance(v, dict):
                    for item in v.values():
                        if not isinstance(item, dict):
                            continue
                        if item.get('listingType') == 'project':
                            continue
                        lm = item.get('listingModel')
                        if isinstance(lm, dict):
                            # Inject id from outer wrapper so we can build url
                            if 'id' not in lm and item.get('id'):
                                lm['id'] = item.get('id')
                            out.append(lm)
                elif k == 'UPVSoldListings' and isinstance(v, list):
                    # Sold results on a sold page live here
                    for item in v:
                        if isinstance(item, dict):
                            lm = item.get('listingModel') or item
                            if isinstance(lm, dict):
                                if 'id' not in lm and item.get('id'):
                                    lm['id'] = item.get('id')
                                out.append(lm)
                else:
                    visit(v, k)
        elif isinstance(x, list):
            for v in x:
                visit(v, parent_key)
    visit(blob)
    return out


def _extract_photo(model):
    """Find a hero photo URL in a Domain listing model.

    Domain's listing models embed media items as nested {type: 'photo', url: ...}
    dicts somewhere in the tree. We walk the model looking for the first such
    node and return its URL. Returns '' if no photo found.

    Why this lives in the pipeline rather than the browser: the previous
    runtime path used corsproxy.io to fetch the listing detail page from each
    swipe-deck card on demand, which broke when corsproxy.io became paid-only
    and Domain started blocking other free CORS proxies. Capturing the URL
    once during the daily scrape avoids the runtime dependency entirely —
    the browser then renders the photo via a plain <img src> which doesn't
    need CORS.
    """
    if not isinstance(model, dict):
        return ''

    found = []

    def walk(o):
        if found:
            return
        if isinstance(o, dict):
            t = o.get('type', '')
            if isinstance(t, str) and t.lower() == 'photo':
                u = o.get('url') or o.get('imageUrl') or o.get('href')
                if isinstance(u, str) and u.startswith('http'):
                    found.append(u)
                    return
            for v in o.values():
                walk(v)
                if found:
                    return
        elif isinstance(o, list):
            for v in o:
                walk(v)
                if found:
                    return

    walk(model)
    if found:
        return found[0]

    # Fallback: search the JSON-stringified model for the Domain bucket-image
    # URL pattern. Not guaranteed to be a hero photo but better than nothing.
    try:
        blob = json.dumps(model, default=str)
        m = re.search(
            r'https?://[\w.-]+(?:domain\.com\.au|domainstatic\.com\.au)[^"\s\',]*',
            blob,
        )
        if m and any(ext in m.group(0).lower() for ext in ('jpg', 'jpeg', 'png', 'webp')):
            return m.group(0)
    except Exception:
        pass
    return ''


def _clean_listing(model, sold=False):
    """
    Normalise a Domain listingModel dict → wash-compatible record.
    Shape (for sale listings):
      {
        "url": "/8-12-gurrigal-street-mosman-nsw-2088-2019049087",
        "price": "$2,950,000",
        "address": {"street": "...", "suburb": "...", "state": "NSW", "postcode": "..."},
        "features": {"beds": 2, "baths": 2, "parking": 1, "propertyType": "House",
                     "propertyTypeFormatted": "House", "landSize": 0, "landUnit": "m²"},
        "id": 2019049087
      }
    """
    try:
        a = model.get('address') or {}
        if isinstance(a, dict):
            street   = (a.get('street') or '').strip()
            suburb   = (a.get('suburb') or '').strip()
            state    = (a.get('state') or 'NSW').strip()
            postcode = (a.get('postcode') or '').strip()
        else:
            street = str(a).strip()
            suburb = state = postcode = ''

        if not street:
            # Fallback to displayAddress if present
            street = (model.get('displayAddress') or '').strip()

        if not street:
            return None

        # Compose full address (Domain format)
        full_parts = [street]
        if suburb:
            tail = suburb
            if state:
                tail += f', {state}'
            if postcode:
                tail += f' {postcode}'
            full_parts.append(tail)
        full_address = ', '.join(full_parts)

        feats = model.get('features') or {}
        beds    = feats.get('beds')
        baths   = feats.get('baths')
        parking = feats.get('parking')
        land_raw  = feats.get('landSize')
        land_unit = feats.get('landUnit') or 'm²'
        if land_raw in (0, '0', None, ''):
            land_size_str = ''
        else:
            land_size_str = f'{land_raw} {land_unit}'.strip()

        ptype_raw = feats.get('propertyTypeFormatted') or feats.get('propertyType') or ''
        ptype = _norm_type(ptype_raw)

        # Price
        price = model.get('price') or ''
        if isinstance(price, dict):
            price = price.get('display') or price.get('displayPrice') or ''
        price = str(price).strip()

        # URL — turn /foo-nsw-2088-12345 into canonical absolute URL
        url = model.get('url') or ''
        if url and url.startswith('/'):
            url = 'https://www.domain.com.au' + url
        # Filter out project URLs (they don't have a numeric id)
        if url and not re.search(r'-\d{9,12}(?:/|$)', url):
            url = ''

        # Sold extras — sold pages embed these under `tags`/`badge`
        sold_price = ''
        sold_date  = ''
        method     = ''
        if sold:
            tags = model.get('tags') or {}
            # On sold pages the price field holds "$1,200,000" or
            # "Price Withheld"; sold date tends to be in badge or similar
            sold_price = price
            badge = model.get('badge') or {}
            if isinstance(badge, dict):
                sold_date = badge.get('soldDate') or ''
            # Also check soldData
            sd = model.get('soldData') or {}
            if isinstance(sd, dict):
                sold_date = sold_date or sd.get('soldDate') or ''
                method    = sd.get('soldAction') or ''

        out = {
            'address':      full_address,
            'suburb':       suburb,
            'beds':         str(beds) if beds not in (None, '', 0) else '',
            'baths':        str(baths) if baths not in (None, '', 0) else '',
            'parking':      str(parking) if parking not in (None, '', 0) else '',
            'propertyType': ptype,
            'landSize':     land_size_str,
            'url':          url,
            'agentNames':   '',   # not reliably present in listingsMap
            'agencyName':   '',
            'heroPhoto':    _extract_photo(model),
        }
        if sold:
            out['soldPrice'] = sold_price
            out['soldDate']  = sold_date
            out['method']    = method
        else:
            out['price'] = price

        return out
    except Exception:
        return None


def _extract_from_next_data(page_html):
    """Pull listings out of Domain's Next.js __NEXT_DATA__ script block."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                  page_html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    return _find_listings_in_json(data)


def _parse_dom_cards(page, sold):
    """Fallback: parse property cards directly from the rendered DOM."""
    cards = page.query_selector_all(
        '[data-testid="listing-card-wrapper-premiumplus"], '
        '[data-testid="listing-card-wrapper-standard"], '
        '[data-testid="listing-card-wrapper-elite"], '
        'li[data-testid^="listing-card"]'
    )
    out = []
    for c in cards:
        try:
            def q(sel):
                el = c.query_selector(sel)
                return el.inner_text().strip() if el else ''

            addr = q('[data-testid="address-line1"]') or q('h2')
            addr2 = q('[data-testid="address-line2"]')
            if addr2:
                addr = f'{addr}, {addr2}'
            if not addr:
                continue

            price = q('[data-testid="listing-card-price"]') or q('[class*="price"]')

            feat_spans = c.query_selector_all(
                '[data-testid="property-features-text-container"] span, '
                '[data-testid="property-features-text"]'
            )
            feat_text = ' '.join(s.inner_text().strip() for s in feat_spans)
            beds = baths = parking = ''
            m = re.search(r'(\d+)\s*(?:Bed|bd)', feat_text, re.I)
            if m: beds = m.group(1)
            m = re.search(r'(\d+)\s*(?:Bath|ba)', feat_text, re.I)
            if m: baths = m.group(1)
            m = re.search(r'(\d+)\s*(?:Parking|Car|Garage)', feat_text, re.I)
            if m: parking = m.group(1)

            # Property type often appears as a span before features
            ptype_el = c.query_selector('[data-testid="property-features-property-type"]')
            ptype = ptype_el.inner_text().strip() if ptype_el else ''
            ptype = _norm_type(ptype)

            link_el = c.query_selector('a[href*="/"]')
            url = ''
            if link_el:
                url = link_el.get_attribute('href') or ''
                if url.startswith('/'):
                    url = 'https://www.domain.com.au' + url

            rec = {
                'address': addr,
                'suburb': '',
                'beds': beds,
                'baths': baths,
                'parking': parking,
                'propertyType': ptype,
                'landSize': '',
                'url': url,
                'agentNames': '',
                'agencyName': '',
            }
            if sold:
                rec['soldPrice'] = price
                rec['soldDate'] = ''
                rec['method'] = ''
            else:
                rec['price'] = price
            out.append(rec)
        except Exception:
            continue
    return out


# ── Core scraper ─────────────────────────────────────────────────────────────

# Module-level counters for the Access-Denied diagnostic. When Domain's Akamai
# edge blocks the scraper it returns a ~330-byte "Access Denied" HTML page
# instead of a real listing page. Previously the scraper silently continued
# with an empty result set — we'd end the day with "0 listings" and no
# indication it was a block versus a legitimate empty market. Now we count
# denials per run and print a summary so the log makes the block obvious.
_denied_pages = 0
_ok_pages = 0
_scrapfly_credits = 0   # running total so the log shows what a run costs


def _get_scrapfly_key():
    """Scrapfly API key from env or .mm_credentials. Empty string = disabled."""
    key = os.environ.get('SCRAPFLY_API_KEY', '').strip()
    if key:
        return key
    creds = SCRIPT_DIR / '.mm_credentials'
    if creds.exists():
        for line in creds.read_text(encoding='utf-8').splitlines():
            if line.startswith('SCRAPFLY_API_KEY='):
                return line.split('=', 1)[1].strip()
    return ''


def _fetch_via_scrapfly(url, api_key):
    """Fetch a Domain page through Scrapfly's anti-bot proxy.

    Domain sits behind Akamai, which denied every request from GitHub
    Actions IPs — plain Playwright, Patchright drop-in, and Patchright's
    full stealth mode (real Chrome + persistent context) were all blocked
    100%. Scrapfly's ASP (Anti Scraping Protection) rotates residential
    IPs and solves the challenge server-side.

    asp=true is the whole point — without it this is just a proxy and
    Akamai blocks it the same as everything else. country=au because
    Domain serves/validates AU traffic. We don't ask for render_js: the
    listing data lives in the __NEXT_DATA__ blob in the initial HTML, so
    JS rendering would cost extra credits for nothing.

    Returns the page HTML, or None on failure (caller falls back / counts
    it as a denial).
    """
    global _scrapfly_credits
    params = urllib.parse.urlencode({
        'key': api_key,
        'url': url,
        'asp': 'true',
        'country': 'au',
        'retry': 'true',
    })
    req = urllib.request.Request(f'https://api.scrapfly.io/scrape?{params}')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:200]
        print(f'      ⚠ scrapfly HTTP {e.code}: {body}')
        return None
    except Exception as e:
        print(f'      ⚠ scrapfly error: {e}')
        return None

    result = payload.get('result') or {}
    cost = (payload.get('context') or {}).get('cost') or result.get('cost') or {}
    if isinstance(cost, dict):
        _scrapfly_credits += int(cost.get('total') or 0)
    status = result.get('status_code')
    if status and status != 200:
        print(f'      ⚠ scrapfly upstream status {status}')
    return result.get('content')


def _is_access_denied(html):
    if not html:
        return True
    if len(html) < 5000 and 'Access Denied' in html:
        return True
    if 'errors.edgesuite.net' in html:  # Akamai signature
        return True
    return False


def scrape_suburb(context, slug, state, postcode, sold, max_pages, scrapfly_key=''):
    """Scrape one suburb through N pages on Domain.

    When scrapfly_key is set every page is fetched through Scrapfly's ASP
    proxy and `context` is unused (no browser is launched at all).
    """
    global _denied_pages, _ok_pages
    results = []
    display = slug.replace('-', ' ').title()

    for page_num in range(1, max_pages + 1):
        if sold:
            # sold-listings works with sort=solddate-desc
            url = (f'https://www.domain.com.au/sold-listings/{slug}-'
                   f'{state.lower()}-{postcode}/?sort=solddate-desc'
                   f'&page={page_num}')
        else:
            # sale page: sort=date-updated-desc triggers a 404, so use default
            url = (f'https://www.domain.com.au/sale/{slug}-'
                   f'{state.lower()}-{postcode}/?excludeunderoffer=0'
                   f'&page={page_num}')

        print(f'    page {page_num}: {url[:90]}…')

        got = []
        page = None
        try:
            if scrapfly_key:
                # Scrapfly path — no browser at all. Their ASP layer handles
                # the Akamai challenge and returns the finished HTML.
                html = _fetch_via_scrapfly(url, scrapfly_key)
                if html is None:
                    # Leave html as None: the _is_access_denied check below
                    # counts it once. (Setting it to '' here double-counted.)
                    html = None
            else:
                page = context.new_page()
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                # Wait for listings to render (or the "no results" marker)
                try:
                    page.wait_for_selector(
                        '[data-testid="address-line1"], '
                        '[data-testid="listing-card-wrapper-premiumplus"], '
                        '[data-testid="listing-card-wrapper-standard"], '
                        '[data-testid="listing-card-wrapper-elite"], '
                        '[data-testid="error-page__main"]',
                        timeout=12000
                    )
                except PWTimeout:
                    pass

                # Brief extra settle for __NEXT_DATA__
                page.wait_for_timeout(1500)

                html = page.content()

            # Detect the ~330-byte Akamai "Access Denied" response BEFORE we
            # bother parsing. When this fires there's no __NEXT_DATA__ to look
            # at — the parse would just silently produce zero listings.
            if _is_access_denied(html):
                _denied_pages += 1
                print(f'      🚫 BLOCKED / no content ({len(html or "")} bytes)')
            else:
                _ok_pages += 1
                # Prefer structured JSON
                listings = _extract_from_next_data(html)
                if listings:
                    for lm in listings:
                        rec = _clean_listing(lm, sold=sold)
                        if rec:
                            if not rec['suburb']:
                                rec['suburb'] = display
                            got.append(rec)

                # DOM fallback — only possible with a live Playwright page.
                # On the Scrapfly path we rely on __NEXT_DATA__, which is
                # present in the initial HTML Domain serves.
                if not got and page is not None:
                    got = _parse_dom_cards(page, sold)
                    for r in got:
                        if not r.get('suburb'):
                            r['suburb'] = display

        except PWTimeout:
            print(f'      ⚠ timeout')
        except Exception as e:
            print(f'      ⚠ error: {e}')
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass

        print(f'      +{len(got)} listings')
        results.extend(got)

        if len(got) == 0:
            break  # no more pages

        # Polite throttle between pages
        time.sleep(random.uniform(1.2, 2.5))

    return results


# ── Output formatting ────────────────────────────────────────────────────────

def dedupe(items, sold=False):
    seen = set()
    out = []
    for p in items:
        addr = (p.get('address') or '').strip().lower()
        suburb = (p.get('suburb') or '').strip().lower()
        key = f'{addr}|{suburb}'
        if sold:
            key += '|' + (p.get('soldDate') or '')
        if addr and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def merge_with_existing(new_items, existing_path, sold=False):
    """
    Merge new results with the existing cache file so we only grow coverage.
    New entries overwrite old ones with the same address (they're fresher),
    but old entries not re-fetched this run are kept.
    """
    existing = []
    if existing_path.exists():
        try:
            prev = json.loads(existing_path.read_text(encoding='utf-8'))
            if isinstance(prev, list):
                existing = prev
        except Exception:
            pass

    def addr_key(p):
        return (p.get('address') or '').strip().lower()

    new_keys = {addr_key(p) for p in new_items if addr_key(p)}
    kept = [p for p in existing if addr_key(p) and addr_key(p) not in new_keys]
    merged = new_items + kept
    return dedupe(merged, sold=sold)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sold-only',   action='store_true')
    ap.add_argument('--listed-only', action='store_true')
    ap.add_argument('--pages',       type=int, default=3,
                    help='How many pages per suburb (default 3)')
    ap.add_argument('--suburbs',     nargs='+')
    ap.add_argument('--no-merge',    action='store_true',
                    help="Overwrite existing cache instead of merging")
    ap.add_argument('--no-scrapfly', action='store_true',
                    help="Force the local-browser path even if a Scrapfly key is set")
    args = ap.parse_args()

    do_sold   = not args.listed_only
    do_listed = not args.sold_only

    suburbs = LNS_SUBURBS
    if args.suburbs:
        wanted = {s.lower() for s in args.suburbs}
        suburbs = [row for row in LNS_SUBURBS if row[0] in wanted]

    print('=' * 60)
    scrapfly_key = '' if args.no_scrapfly else _get_scrapfly_key()
    fetcher = 'scrapfly (ASP)' if scrapfly_key else f'local browser ({_DRIVER})'
    print(f'  Domain Scraper — via {fetcher}')
    print(f'  {datetime.now().strftime("%A %d %B %Y  %H:%M")}')
    print(f'  {len(suburbs)} suburbs × {args.pages} pages')
    print(f'  listed={do_listed}  sold={do_sold}')
    print('=' * 60)

    all_listed = []
    all_sold   = []

    def run_suburbs(context):
        """Walk every suburb. `context` is None on the Scrapfly path."""
        for slug, state, postcode in suburbs:
            print(f'\n▶ {slug.replace("-", " ").title()}')
            if do_listed:
                print('  ── For Sale ──')
                props = scrape_suburb(context, slug, state, postcode,
                                      sold=False, max_pages=args.pages,
                                      scrapfly_key=scrapfly_key)
                print(f'    total for sale: {len(props)}')
                all_listed.extend(props)
            if do_sold:
                print('  ── Sold ──')
                props = scrape_suburb(context, slug, state, postcode,
                                      sold=True, max_pages=args.pages,
                                      scrapfly_key=scrapfly_key)
                print(f'    total sold: {len(props)}')
                all_sold.extend(props)

    if scrapfly_key:
        # No browser needed at all — Scrapfly returns finished HTML.
        run_suburbs(None)
    else:
        _run_with_browser(run_suburbs)

    _finish(all_listed, all_sold, args, do_listed, do_sold)


def _run_with_browser(run_suburbs):
    """Legacy local-browser path (Patchright / Playwright). Retained as a
    fallback for dev machines and in case Scrapfly is ever unavailable —
    but Domain's Akamai currently denies 100% of requests through it."""
    with sync_playwright() as pw:
        if _DRIVER == 'patchright':
            # Patchright's recommended full-stealth mode: persistent context
            # + real Chrome + NO custom user_agent / viewport / init scripts
            # (they all leak automation signals). Significantly more
            # effective than the drop-in sync_api swap, which Domain's
            # Akamai still fingerprinted and denied every request.
            import tempfile
            user_data_dir = tempfile.mkdtemp(prefix='patchright-')
            context = pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel='chrome',       # real Chrome (installed on runner),
                                        # not the Chromium build Playwright ships
                headless=True,
                no_viewport=True,
                locale='en-AU',
                timezone_id='Australia/Sydney',
            )
            browser = None
        else:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1440, 'height': 900},
                locale='en-AU',
                timezone_id='Australia/Sydney',
            )
            # Light stealth: remove webdriver flag
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

        try:
            run_suburbs(context)
        finally:
            try:
                context.close()
                if browser is not None:
                    browser.close()
            except Exception:
                pass


def _finish(all_listed, all_sold, args, do_listed, do_sold):
    """Dedupe, report, and write the three output files."""
    all_listed = dedupe(all_listed, sold=False)
    all_sold   = dedupe(all_sold, sold=True)

    print('\n' + '=' * 60)
    total_pages = _denied_pages + _ok_pages
    if total_pages:
        print(f'  Pages: {_ok_pages} ok, {_denied_pages} denied '
              f'({_denied_pages*100//total_pages}% blocked)')
    if _scrapfly_credits:
        # ~30 days of this figure is the monthly credit burn — worth
        # watching against the plan's allowance.
        print(f'  Scrapfly credits used this run: {_scrapfly_credits:,}')
    print(f'  New for-sale: {len(all_listed)}')
    print(f'  New sold    : {len(all_sold)}')

    # ── Write flat wash-compatible caches ─────────────────────────────────
    if do_listed:
        final_fs = all_listed if args.no_merge else merge_with_existing(
            all_listed, OUTPUT_FORSALE, sold=False
        )
        OUTPUT_FORSALE.write_text(
            json.dumps(final_fs, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )
        print(f'  Saved {OUTPUT_FORSALE.name}: {len(final_fs)} total')

    if do_sold:
        final_sold = all_sold if args.no_merge else merge_with_existing(
            all_sold, OUTPUT_SOLD, sold=True
        )
        OUTPUT_SOLD.write_text(
            json.dumps(final_sold, indent=2, ensure_ascii=False),
            encoding='utf-8',
        )
        print(f'  Saved {OUTPUT_SOLD.name}: {len(final_sold)} total')

    # ── Legacy combined output (inject_offmarket.py consumer) ─────────────
    legacy = {
        'scraped_at': datetime.now().isoformat(),
        'date': datetime.now().strftime('%d %b %Y'),
        'newly_listed': all_listed,
        'sold': all_sold,
        'counts': {
            'newly_listed': len(all_listed),
            'sold': len(all_sold),
        }
    }
    OUTPUT_LEGACY.write_text(
        json.dumps(legacy, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )
    print(f'  Saved {OUTPUT_LEGACY.name}')
    print('=' * 60)


if __name__ == '__main__':
    main()
