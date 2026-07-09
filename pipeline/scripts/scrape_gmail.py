#!/usr/bin/env python3
"""
scrape_gmail.py — Scrape Gmail IMAP for off-market & Proping emails.

Connects via IMAP using App Password stored in macOS Keychain.
Scans for:
  1. Proping daily report emails (auto-parses property data)
  2. Off-market / pre-market agent emails
  3. Any real estate agent correspondence

Output:
  - gmail_offmarket.json     — off-market property emails
  - gmail_proping_raw.json   — raw Proping email data for injection
  - gmail_scan_log.json      — scan state (last scan date, message IDs)

Usage:
  python3 scrape_gmail.py                # Full scan (last 30 days)
  python3 scrape_gmail.py --days 7       # Scan last 7 days
  python3 scrape_gmail.py --proping-only # Only scan for Proping emails
  python3 scrape_gmail.py --offmarket-only # Only scan for off-market
"""

import imaplib
import email
from email.header import decode_header
import json
import re
import os
import sys
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from html.parser import HTMLParser

SCRIPT_DIR = Path(__file__).parent
DOWNLOADS = SCRIPT_DIR.parent

# Output files — MUST match what inject_email_data.py reads
OFFMARKET_OUT = DOWNLOADS / 'offmarket_emails.json'
PROPING_OUT = DOWNLOADS / 'proping_history.json'
# Legacy filenames (symlinked for backward compat)
_LEGACY_PROPING = DOWNLOADS / 'gmail_proping_raw.json'
_LEGACY_OFFMKT = DOWNLOADS / 'gmail_offmarket.json'
SCAN_LOG = SCRIPT_DIR / 'gmail_scan_log.json'

# Gmail IMAP settings
IMAP_HOST = 'imap.gmail.com'
IMAP_PORT = 993
EMAIL_ADDR = 'mazarmartinapp@gmail.com'
KEYCHAIN_SERVICE = 'mm-gmail-imap'

# Scan settings
DEFAULT_SCAN_DAYS = 90
MAX_EMAILS = 500

# ── Off-market keywords ──
OFFMARKET_SUBJECT_KW = [
    'off-market', 'off market', 'offmarket', 'off mkt',
    'pre-market', 'pre market', 'premarket',
    'coming soon', 'pocket listing', 'pocket sale',
    'exclusive listing', 'exclusive opportunity', 'exclusive sale',
    'not on market', 'not yet listed', 'unlisted',
    'silent sale', 'private sale', 'private treaty',
    'quiet sale', 'discreet sale', 'discreet opportunity',
    'expressions of interest', 'eoi',
    'for your buyers', 'buyer opportunity',
    'before it hits the market', 'prior to listing',
    'first look', 'sneak peek', 'sneak preview',
    'not yet on domain', 'not yet advertised',
    'pre listing', 'pre-listing',
]

OFFMARKET_BODY_KW = [
    'off-market opportunity', 'not currently listed',
    'not publicly listed', 'before going to market',
    'prior to going to market', 'private off-market',
    'exclusively available', 'available before listing',
    'quietly available', 'not on the open market',
    'owner is open to', 'owner willing to sell',
    'vendor has agreed to sell', 'vendor happy to sell privately',
]

# LNS suburbs
LNS_SUBURBS = [
    'mosman', 'cremorne', 'neutral bay', 'north sydney', 'kirribilli',
    'milsons point', 'waverton', 'wollstonecraft', 'crows nest', 'st leonards',
    'naremburn', 'cammeray', 'northbridge', 'castlecrag', 'willoughby',
    'artarmon', 'chatswood', 'lane cove', 'greenwich', 'longueville',
    'riverview', 'linley point', 'hunters hill', 'woolwich',
    'mcmahons point', 'lavender bay', 'kurraba point',
    'cremorne point', 'clifton gardens',
]


class HTMLStripper(HTMLParser):
    """Strip HTML tags and return plain text.

    Preserves <img src="..."> tags as inline [img:URL] markers so the
    Proping parser can pair each property with its photo. Domain scraping
    is blocked at Akamai — the Proping email is our only reliable photo
    source.
    """
    # Signatures that identify tracking pixels, analytics beacons, and
    # layout spacers. Real property photos never contain any of these.
    _TRACKING_SIGS = ('pixel', 'beacon', 'track', 'analytics',
                      'open?', '/open/', 'spacer', 'blank.gif')

    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data)
    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            a = dict(attrs)
            # Some emails put the real URL on data-src for lazy loading and
            # leave src as a placeholder; check both.
            src = a.get('src') or a.get('data-src') or ''
            if not src.startswith(('http://', 'https://')):
                return
            low = src.lower()
            if any(sig in low for sig in self._TRACKING_SIGS):
                return
            # Reject obvious 1x1 spacers referenced by dimension query args.
            if 'w=1' in low or 'width=1' in low or 'h=1' in low or 'height=1' in low:
                return
            self.text.append(f' [img:{src}] ')
    def get_text(self):
        return ' '.join(self.text)


def strip_html(html_str):
    s = HTMLStripper()
    s.feed(html_str)
    return s.get_text()


def get_app_password():
    """Retrieve App Password from env var, credentials file, or macOS Keychain."""
    # 1. Check environment variable
    pw = os.environ.get('GMAIL_APP_PASSWORD', '').strip()
    if pw:
        return pw
    # 2. Check .mm_credentials file (for sandbox / scheduled tasks)
    creds_file = Path(__file__).parent / '.mm_credentials'
    if creds_file.exists():
        for line in creds_file.read_text().splitlines():
            if line.startswith('GMAIL_APP_PASSWORD='):
                pw = line.split('=', 1)[1].strip()
                if pw:
                    return pw
    # 3. Fall back to macOS Keychain
    try:
        pw = subprocess.check_output([
            'security', 'find-generic-password',
            '-a', EMAIL_ADDR,
            '-s', KEYCHAIN_SERVICE, '-w'
        ]).decode().strip()
        return pw
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("  ❌ App Password not found in env, .mm_credentials, or Keychain.")
        print(f"     Run: security add-generic-password -a '{EMAIL_ADDR}' -s '{KEYCHAIN_SERVICE}' -w '<app-password>'")
        sys.exit(1)


def connect_gmail():
    """Connect to Gmail IMAP and return the mail object."""
    pw = get_app_password()
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    result, _ = mail.login(EMAIL_ADDR, pw)
    if result != 'OK':
        print(f"  ❌ Login failed: {result}")
        sys.exit(1)
    return mail


def decode_mime_header(raw):
    """Decode a MIME-encoded email header."""
    if not raw:
        return ''
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or 'utf-8', errors='ignore'))
        else:
            decoded.append(part)
    return ' '.join(decoded)


def get_email_body(msg):
    """Extract body text from email message.

    Prefers the HTML part (run through HTMLStripper) because that path
    preserves inline <img src="..."> URLs as [img:URL] markers so the
    Proping parser can attach property photos. Falls back to text/plain
    only if no HTML part exists.
    """
    body = ''
    if msg.is_multipart():
        html_body = ''
        text_body = ''
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    text_body += payload.decode(charset, errors='ignore')
            elif ct == 'text/html' and not html_body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    html_body = strip_html(payload.decode(charset, errors='ignore'))
        body = html_body or text_body
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or 'utf-8'
            text = payload.decode(charset, errors='ignore')
            if ct == 'text/html':
                body = strip_html(text)
            else:
                body = text
    return body.strip()


def parse_email_date(msg):
    """Parse email date to DD/MM/YYYY in local timezone."""
    date_str = msg.get('Date', '')
    if not date_str:
        return datetime.today().strftime('%d/%m/%Y')
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        # Convert to local timezone so UTC midnight emails get the right local date
        dt = dt.astimezone()
        return dt.strftime('%d/%m/%Y')
    except Exception:
        return datetime.today().strftime('%d/%m/%Y')


TRUSTED_PROPING_SENDERS = (
    'mon mazar', 'monmazar',
    'gerard mazar', 'gerardmazar',
    'jeremy martin', 'jeremymartin',
)


def is_proping_email(subject, sender, body=''):
    """Check if this is a Proping daily report email.

    Recognises both direct Proping emails and forwards from the three
    trusted senders: Mon Mazar, Gerard Mazar, Jeremy Martin.
    """
    s = (subject or '').lower()
    e = (sender or '').lower()
    b = (body or '')[:5000].lower()

    # Direct indicators
    if ('proping' in s or 'proping' in e or
            'property intelligence' in s or
            '@proping.com.au' in e or
            'proping@proping.com.au' in b or
            'daily property report' in s or
            'daily snapshot' in s or
            'changes in your market' in s or
            ('ping!!' in s and 'changes' in s)):
        return True

    # Forwards from trusted senders — require a Proping body signature
    if any(ts in e for ts in TRUSTED_PROPING_SENDERS):
        if ('proping' in b or
                'changes in your market' in b or
                'property intelligence' in b or
                'newly listed' in b or
                'price change' in b or
                'auction changes' in b):
            return True

    return False


def is_offmarket_email(subject, body):
    """Check if this is an off-market / pre-market email."""
    s = (subject or '').lower()
    b = (body or '')[:5000].lower()

    for kw in OFFMARKET_SUBJECT_KW:
        if kw in s:
            return True
    for kw in OFFMARKET_BODY_KW:
        if kw in b:
            return True
    return False


def parse_proping_email(subject, body, date_str):
    """Parse a Proping daily report email into structured data.

    Proping emails have this format (plain text with URLs stripped):
        Newly Listed(8)
        [image url]
        2/81A Glover Street, Mosman<domain_url>
        2 bed0 Days listed<domain_url>
        $1,900,000 <domain_url> Proping Estimate*
        Adam Vernon / Vernon Partners<domain_url>

        Price Change(3)
        209/1A Eden Street, North Sydney<domain_url>
        1 bed42 Days listed
        $790,000  Price Guide  $90,000  (the price change amount)
        Leonie Wells / Wells Real Estate

        Sold(5)
        ...
        Auction Change(3)
        ...
        Unlisted(3)
        ...
        Over 90 Days(1)
        ...
    """
    data = {
        'date': date_str,
        'newly_listed': [],
        'price_changes': [],
        'sold': [],
        'auction_changes': [],
        'unlisted': [],
        'over_90_days': [],
        'source': 'gmail_proping',
    }

    # Strip URLs: <http...> and [http...]
    cleaned = re.sub(r'<https?://[^>]+>', '', body)
    cleaned = re.sub(r'\[https?://[^\]]+\]', '', cleaned)
    # Extract image URLs from [img:URL] (our HTMLStripper) and [image: URL]
    # (Proping's plain-text placeholder). Replace with a compact marker
    # the parser recognizes, so we can attach each image to the property
    # that follows it in the email layout.
    def _mark_img(m):
        url = m.group(1).strip()
        return f' __MMIMG__{url}__MMIMG__ '
    cleaned = re.sub(r'\[img:(https?://[^\]]+)\]', _mark_img, cleaned)
    cleaned = re.sub(r'\[image:\s*(https?://[^\]]+)\]', _mark_img, cleaned)

    lines = cleaned.split('\n')

    # Section header patterns: "Newly Listed(8)", "Price Change(3)", "Sold(5)", etc.
    section_map = {
        'newly listed': 'newly_listed',
        'new listing': 'newly_listed',
        'price change': 'price_changes',
        'price reduction': 'price_changes',
        'sold': 'sold',
        'auction change': 'auction_changes',
        'unlisted': 'unlisted',
        'withdrawn': 'unlisted',
        'over 90 days': 'over_90_days',
        '90+ days': 'over_90_days',
    }

    current_section = None
    current_entry = None
    pending_img = None  # last image URL seen before the next address line
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line:
            continue

        # Capture image markers (from HTML img tags or Proping's plain-text
        # placeholders). Remember the URL so the next address line inherits
        # it as its heroPhoto. Multiple images before one address = we
        # keep only the LAST one, which in Proping's layout is the hero.
        img_matches = re.findall(r'__MMIMG__(https?://\S+?)__MMIMG__', line)
        if img_matches:
            pending_img = img_matches[-1]
            # Strip the markers so the rest of the line can still parse
            # normally (Proping sometimes inlines an image + text together).
            line = re.sub(r'__MMIMG__\S+?__MMIMG__', '', line).strip()
            if not line:
                continue

        # Check for section headers like "Newly Listed(8)" or "Sold(5)"
        l_lower = line.lower()
        matched_section = False
        for key, section in section_map.items():
            if key in l_lower and re.search(r'\(\d+\)', line):
                current_section = section
                matched_section = True
                # Save any pending entry
                if current_entry and current_entry.get('address'):
                    data[current_entry['_section']].append(
                        {k: v for k, v in current_entry.items() if k != '_section'})
                current_entry = None
                pending_img = None  # reset between sections
                break
        if matched_section:
            continue

        if not current_section:
            continue

        # Skip stray URLs and decorative lines
        if line.startswith('[') or line.startswith('http') or line == '________________________________':
            continue

        # Try to match an address line: "2/81A Glover Street, Mosman" or "Level 4, 406/53 Palmer Street, Cammeray"
        addr_m = re.match(
            r'(?:Level\s+\d+,?\s*)?'
            r'(\d+[A-Za-z]?(?:/\d+[A-Za-z]?)?\s+[A-Za-z][A-Za-z\s\']+?'
            r'(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Place|Pl|Court|Ct|Way|Close|Cl'
            r'|Crescent|Cres|Boulevard|Blvd|Parade|Pde|Highway|Hwy|Terrace|Tce|Circuit|Cct'
            r'|Walk|Grove|Track|Ridge|Glen|Rise|View|Row)'
            r',\s*[A-Z][a-zA-Z\s]+)',
            line, re.I
        )
        if addr_m:
            # Save previous entry
            if current_entry and current_entry.get('address'):
                data[current_entry['_section']].append(
                    {k: v for k, v in current_entry.items() if k != '_section'})

            addr = addr_m.group(0).strip()
            # Include "Level X," prefix if present
            if line.lower().startswith('level'):
                level_m = re.match(r'(Level\s+\d+,?\s*)', line, re.I)
                if level_m:
                    addr = level_m.group(1) + addr

            addr = re.sub(r'\s+(?:NSW|nsw)\s*\d{4}$', '', addr).strip()
            suburb = ''
            parts = addr.split(',')
            if len(parts) > 1:
                suburb = parts[-1].strip()

            current_entry = {
                'address': addr,
                'suburb': suburb,
                'beds': '',
                'days_listed': '',
                'price': '',
                'agent': '',
                'agency': '',
                'source': 'proping_email',
                'date': date_str,
                'heroPhoto': pending_img or '',
                '_section': current_section,
            }
            pending_img = None  # consumed by this entry
            continue

        if not current_entry:
            continue

        # Match beds + days listed: "2 bed0 Days listed" or "3 bed42 Days listed"
        beds_m = re.match(r'(\d+)\s*bed\s*(\d+)\s*[Dd]ays?\s*listed', line)
        if beds_m:
            current_entry['beds'] = beds_m.group(1)
            current_entry['days_listed'] = beds_m.group(2)
            continue

        # Match price line: "$1,900,000" possibly with "Proping Estimate*" or "Price Guide"
        price_m = re.match(r'(\$[\d,]+(?:\.\d+)?)', line)
        if price_m:
            current_entry['price'] = price_m.group(1)
            # Check for price change amount (for Price Change section)
            if current_section == 'price_changes':
                # Match "$-199,000" or "$+50,000" or just "$199,000" after the main price
                change_m = re.search(r'(\$-[\d,]+)', line)
                if change_m:
                    current_entry['price_change'] = change_m.group(1)
                else:
                    # Try unsigned amount at end (different from main price)
                    change_m2 = re.search(r'(\$[\d,]+)\s*(?:<[^>]*>)?\s*$', line)
                    if change_m2 and change_m2.group(1) != price_m.group(1):
                        current_entry['price_change'] = change_m2.group(1)
            # Check for sold price
            if current_section == 'sold':
                sold_m = re.search(r'Sold\s+(?:for\s+)?(\$[\d,]+)', line, re.I)
                if sold_m:
                    current_entry['sold_price'] = sold_m.group(1)
                elif 'price withheld' in line.lower():
                    current_entry['sold_price'] = 'Price Withheld'
                else:
                    current_entry['sold_price'] = 'Price Withheld'
            continue

        # Match agent line: "Adam Vernon / Vernon Partners" or "Agent Name / Agency Name"
        agent_m = re.match(r'([A-Z][a-zA-Z\s\(\)]+?)\s*/\s*([A-Z][a-zA-Z\s&\'\-\.]+)', line)
        if agent_m:
            current_entry['agent'] = agent_m.group(1).strip()
            current_entry['agency'] = agent_m.group(2).strip()
            continue

        # Match auction change details: "New Auction 02 May" or "Pushed 14 days"
        if current_section == 'auction_changes':
            auc_m = re.search(r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*)', line, re.I)
            if auc_m:
                current_entry['new_auction_date'] = auc_m.group(1)
            pushed_m = re.search(r'(?:pushed|delayed)\s*(\d+)\s*days?', line, re.I)
            if pushed_m:
                current_entry['pushed_days'] = pushed_m.group(1)

    # Save last entry
    if current_entry and current_entry.get('address'):
        data[current_entry['_section']].append(
            {k: v for k, v in current_entry.items() if k != '_section'})

    return data


def parse_offmarket_email(subject, body, sender_name, sender_email, date_str):
    """Parse an off-market email into a property dict."""
    prop = {
        'address': '',
        'suburb': '',
        'beds': '',
        'baths': '',
        'cars': '',
        'land': '',
        'price': '',
        'propertyType': '',
        'agent': sender_name,
        'agency': '',
        'notes': '',
        'source': 'gmail',
        'date': date_str,
        'email_subject': subject,
        'sender_email': sender_email,
    }

    combined = (subject or '') + '\n' + (body or '')

    # Address
    addr_m = re.search(
        r'(\d+[A-Za-z]?[/\d]*\s+[A-Z][a-zA-Z\s\']+(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr'
        r'|Lane|Ln|Place|Pl|Court|Ct|Way|Close|Cl|Crescent|Cres|Boulevard|Blvd|Parade|Pde'
        r'|Highway|Hwy|Terrace|Tce|Circuit|Cct|Walk|Grove|Track|Ridge|Glen|Rise|View|Row)'
        r'[\.,]?\s*[A-Z][a-zA-Z\s]+)',
        combined
    )
    if addr_m:
        addr = addr_m.group(1).strip().rstrip(',').strip()
        addr = re.sub(r'\s+(?:NSW|VIC|QLD|SA|WA|TAS|ACT|NT)\s*\d*$', '', addr).strip()
        prop['address'] = addr
        parts = addr.split(',')
        if len(parts) > 1:
            prop['suburb'] = parts[-1].strip()

    # Property type
    type_patterns = [
        (r'\b(?:house|family home|freestanding)\b', 'House'),
        (r'\b(?:apartment|apt|unit|flat)\b', 'Apartment'),
        (r'\b(?:townhouse|town house|terrace)\b', 'Townhouse'),
        (r'\b(?:duplex|semi[- ]detached|semi)\b', 'Duplex'),
        (r'\b(?:villa)\b', 'Villa'),
        (r'\b(?:land|vacant land|block)\b', 'Land'),
        (r'\b(?:penthouse)\b', 'Penthouse'),
    ]
    for pattern, ptype in type_patterns:
        if re.search(pattern, combined, re.I):
            prop['propertyType'] = ptype
            break

    # Beds/baths/parking
    compact = re.search(r'(\d+)\s*bed.*?(\d+)\s*bath.*?(\d+)\s*(?:car|park|garage)', body or '', re.I)
    if compact:
        prop['beds'] = compact.group(1)
        prop['baths'] = compact.group(2)
        prop['cars'] = compact.group(3)
    else:
        bed_m = re.search(r'(\d+)\s+bed(?:room)?s?', combined, re.I)
        if bed_m: prop['beds'] = bed_m.group(1)
        bath_m = re.search(r'(\d+)\s+bath(?:room)?s?', combined, re.I)
        if bath_m: prop['baths'] = bath_m.group(1)
        car_m = re.search(r'(\d+)\s+(?:parking|garage|car\s*space|car)', combined, re.I)
        if car_m: prop['cars'] = car_m.group(1)

    # Land size
    area_m = re.search(r'(\d[\d,]*)\s*(?:sqm|m²|sq\.?\s*m)', combined, re.I)
    if area_m: prop['land'] = area_m.group(1).replace(',', '')

    # Price
    price_m = re.search(r'\$\s*[\d,.]+\s*(?:million|mil|m)?', combined, re.I)
    if price_m: prop['price'] = price_m.group(0).strip()

    # Agency from known names
    agency_names = [
        'DiJones', 'McGrath', 'Ray White', 'LJ Hooker', 'Raine & Horne',
        'Atlas', 'Belle Property', 'Stone Real Estate', 'Phillips Pantzer Donnelley',
        'PPD', 'Richardson & Wrench', 'The Agency', 'BresicWhitney',
        'Cobden & Hayson', 'Cunninghams', 'Laing+Simmons',
        'Sotheby', 'Vernon Partners', 'Northside Realtors',
    ]
    for agency in agency_names:
        if agency.lower() in combined.lower():
            prop['agency'] = agency
            break
    if not prop['agency'] and sender_email:
        domain = sender_email.split('@')[-1].split('.')[0] if '@' in sender_email else ''
        if domain and len(domain) > 2:
            prop['agency'] = domain.title()

    # Notes
    paras = [p.strip() for p in re.split(r'\n{2,}', body or '') if p.strip()]
    for para in paras[:5]:
        if len(para) > 30 and not re.match(r'^[\d\s()+\-]+$', para):
            prop['notes'] = para[:250].replace('\n', ' ')
            break

    return prop


def _dedupe_consecutive_days(days, cats):
    """Remove TZ-bounce duplicates from proping_history.

    Historical Actions runs used UTC while local runs used Sydney, so the same
    Proping email got stamped under two consecutive dates. This pass drops any
    entry on day N that has an identical sibling (same category, same content)
    on day N+1, keeping the later (Sydney-correct) copy. 97% of duplicates in
    the historical data fit this 1-day-apart signature.
    """
    if len(days) < 2:
        return days

    def _parse(s):
        try: return datetime.strptime(s, '%d/%m/%Y')
        except: return None

    # `days_listed` ticks up by 1 each day a listing stays alive, so two consecutive
    # day reports of the same event differ only by that counter. Exclude it (and
    # `date`) from the identity so the dedupe collapses them. Validated against
    # Gerard's deployed index: auction_changes matches 301 == 301.
    DRIFT_FIELDS = {'date', 'days_listed'}

    def _key(cat, e):
        return (cat, tuple(sorted((k, str(v)) for k, v in e.items() if k not in DRIFT_FIELDS and v)))

    by_date = {d['date']: d for d in days if d.get('date')}
    by_dt = {ds: _parse(ds) for ds in by_date}

    # Snapshot next-day keys BEFORE modifying anything so chains of 3+ collapse cleanly.
    next_keys = {}
    for ds in by_date:
        dt = by_dt.get(ds)
        if not dt:
            continue
        nxt = (dt + timedelta(days=1)).strftime('%d/%m/%Y')
        nxt_day = by_date.get(nxt)
        if not nxt_day:
            continue
        next_keys[ds] = {cat: {_key(cat, e) for e in (nxt_day.get(cat) or [])} for cat in cats}

    removed = 0
    for ds, d in by_date.items():
        nk = next_keys.get(ds, {})
        for cat, keys in nk.items():
            if not keys:
                continue
            before = d.get(cat) or []
            after = [e for e in before if _key(cat, e) not in keys]
            removed += len(before) - len(after)
            d[cat] = after

    if removed:
        print(f"  Deduped {removed} TZ-bounce duplicate(s) across consecutive days")
    return days


def load_scan_log():
    """Load previous scan state."""
    if SCAN_LOG.exists():
        try:
            return json.loads(SCAN_LOG.read_text())
        except Exception:
            pass
    return {'last_scan': None, 'seen_ids': []}


def save_scan_log(log):
    """Save scan state."""
    # Keep only last 1000 message IDs
    if len(log.get('seen_ids', [])) > 1000:
        log['seen_ids'] = log['seen_ids'][-1000:]
    SCAN_LOG.write_text(json.dumps(log, indent=2))


def scan_gmail(days=DEFAULT_SCAN_DAYS, proping_only=False, offmarket_only=False):
    """Main scan function."""
    print("=" * 60)
    print("📧 Gmail Email Scanner — Mazar Martin CRM")
    print(f"   {datetime.now().strftime('%A %d %B %Y, %H:%M')}")
    print("=" * 60)

    mail = connect_gmail()
    print(f"  ✅ Connected to {EMAIL_ADDR}")

    # Select INBOX
    mail.select('INBOX')

    # Search for recent emails
    since_date = (datetime.now() - timedelta(days=days)).strftime('%d-%b-%Y')
    result, data = mail.search(None, f'(SINCE {since_date})')
    msg_ids = data[0].split()
    print(f"  📬 {len(msg_ids)} emails in last {days} days")

    if not msg_ids:
        print("  No emails to scan.")
        mail.logout()
        return

    scan_log = load_scan_log()
    seen = set(scan_log.get('seen_ids', []))

    proping_entries = []
    offmarket_entries = []
    skipped = 0
    errors = 0

    for msg_id in msg_ids[-MAX_EMAILS:]:
        msg_id_str = msg_id.decode()

        try:
            result, msg_data = mail.fetch(msg_id, '(RFC822)')
            if result != 'OK':
                errors += 1
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = decode_mime_header(msg.get('Subject', ''))
            from_header = decode_mime_header(msg.get('From', ''))
            date_str = parse_email_date(msg)

            # Extract sender name and email
            sender_name = ''
            sender_email = ''
            from_m = re.match(r'"?([^"<]+)"?\s*<?([^>]*)>?', from_header)
            if from_m:
                sender_name = from_m.group(1).strip().strip('"')
                sender_email = from_m.group(2).strip()
            elif '@' in from_header:
                sender_email = from_header.strip()

            body = get_email_body(msg)

            # Check for Proping email
            if not offmarket_only and is_proping_email(subject, sender_email, body):
                print(f"  📊 Proping: {subject[:60]} ({date_str})")
                parsed = parse_proping_email(subject, body, date_str)
                total = sum(len(parsed[k]) for k in ['newly_listed', 'price_changes', 'sold', 'auction_changes', 'unlisted', 'over_90_days'])
                sample_photo = ''
                photos = 0
                for k in parsed:
                    if not isinstance(parsed[k], list):
                        continue
                    for p in parsed[k]:
                        if isinstance(p, dict) and p.get('heroPhoto'):
                            photos += 1
                            if not sample_photo:
                                sample_photo = p['heroPhoto']
                print(f"     → {total} properties extracted ({photos} with photo)")
                if sample_photo:
                    print(f"        sample photo URL: {sample_photo[:120]}")
                proping_entries.append(parsed)
                seen.add(msg_id_str)
                continue

            # Check for off-market email
            if not proping_only and is_offmarket_email(subject, body):
                prop = parse_offmarket_email(subject, body, sender_name, sender_email, date_str)
                if prop['address']:
                    # Check if LNS
                    is_lns = any(sub in (prop['address'] + ' ' + body[:1000]).lower() for sub in LNS_SUBURBS)
                    if not is_lns:
                        prop['notes'] = (prop.get('notes', '') + ' [non-LNS]').strip()
                    print(f"  🏠 Off-market: {prop['address']} ({date_str})")
                    print(f"     {prop['beds']}bd {prop['baths']}ba | {prop['price']} | {sender_name}")
                    offmarket_entries.append(prop)
                else:
                    print(f"  ⚠️  Off-market email (no address): {subject[:60]}")
                seen.add(msg_id_str)

        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  ⚠️  Error processing email {msg_id_str}: {str(e)[:100]}")

    mail.logout()

    # ── Save results ──
    print(f"\n{'='*60}")
    print(f"  📊 SCAN RESULTS")
    print(f"{'='*60}")

    if proping_entries:
        # Merge with existing
        existing = []
        if PROPING_OUT.exists():
            try:
                existing = json.loads(PROPING_OUT.read_text())
            except: pass

        # Helper: merge two day-entries by combining category lists (dedup by address)
        CATEGORY_KEYS = ['newly_listed', 'price_changes', 'sold', 'auction_changes', 'unlisted', 'over_90_days', 'ninety_plus_days']
        def _merge_day(base, incoming):
            """Merge incoming day-entry into base, combining category lists and deduping by address."""
            for cat in CATEGORY_KEYS:
                base_list = base.get(cat, [])
                inc_list = incoming.get(cat, [])
                if not inc_list:
                    continue
                seen_addrs = {p.get('address', '').lower().strip() for p in base_list}
                for p in inc_list:
                    addr = p.get('address', '').lower().strip()
                    if addr and addr not in seen_addrs:
                        base_list.append(p)
                        seen_addrs.add(addr)
                    elif addr in seen_addrs:
                        # Update existing entry with any new fields from incoming
                        for existing_p in base_list:
                            if existing_p.get('address', '').lower().strip() == addr:
                                for k, v in p.items():
                                    if v and (k not in existing_p or not existing_p[k]):
                                        existing_p[k] = v
                                break
                base[cat] = base_list
            return base

        # First consolidate all scraped entries by date (multiple emails per day)
        consolidated = {}
        for entry in proping_entries:
            d = entry.get('date')
            if d in consolidated:
                _merge_day(consolidated[d], entry)
            else:
                consolidated[d] = entry

        # Now merge consolidated entries with existing saved data
        existing_by_date = {e.get('date'): e for e in existing}
        new_count = 0
        for d, entry in consolidated.items():
            if d in existing_by_date:
                _merge_day(existing_by_date[d], entry)
            else:
                existing_by_date[d] = entry
                new_count += 1

        all_proping = sorted(existing_by_date.values(), key=lambda x: x.get('date', ''), reverse=True)
        all_proping = _dedupe_consecutive_days(all_proping, CATEGORY_KEYS)
        PROPING_OUT.write_text(json.dumps(all_proping, indent=2, ensure_ascii=False))
        total_props = sum(sum(len(e.get(c, [])) for c in CATEGORY_KEYS) for e in consolidated.values())
        print(f"  Proping reports: {len(proping_entries)} scanned, {new_count} new, {len(proping_entries) - new_count} updated")
        print(f"  Total properties across all dates: {total_props}")
        print(f"  Saved → {PROPING_OUT}")
    else:
        print(f"  Proping reports: 0")

    if offmarket_entries:
        # Merge with existing (dedup by address)
        existing = []
        if OFFMARKET_OUT.exists():
            try:
                existing = json.loads(OFFMARKET_OUT.read_text())
            except: pass

        def norm(addr):
            return re.sub(r'[^a-z0-9]', '', (addr or '').lower())

        existing_addrs = {norm(e.get('address', '')) for e in existing if e.get('address')}
        new_off = [e for e in offmarket_entries if norm(e.get('address', '')) not in existing_addrs]
        all_off = new_off + existing
        OFFMARKET_OUT.write_text(json.dumps(all_off, indent=2, ensure_ascii=False))
        print(f"  Off-market: {len(offmarket_entries)} found, {len(new_off)} new")
        print(f"  Saved → {OFFMARKET_OUT}")
    else:
        print(f"  Off-market: 0")

    if errors:
        print(f"  ⚠️  Errors: {errors}")

    # Create legacy symlinks for backward compat
    for src, dst in [(_LEGACY_PROPING, PROPING_OUT), (_LEGACY_OFFMKT, OFFMARKET_OUT)]:
        try:
            if src.is_symlink() or src.exists():
                src.unlink()
            src.symlink_to(dst)
        except Exception:
            pass

    # Update scan log
    scan_log['last_scan'] = datetime.now().isoformat()
    scan_log['seen_ids'] = list(seen)
    save_scan_log(scan_log)

    print(f"\n  ✅ Done!")
    return {
        'proping': len(proping_entries),
        'offmarket': len(offmarket_entries),
        'errors': errors,
    }


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Gmail email scanner for MM CRM')
    parser.add_argument('--days', type=int, default=DEFAULT_SCAN_DAYS, help='Scan last N days')
    parser.add_argument('--proping-only', action='store_true', help='Only scan for Proping emails')
    parser.add_argument('--offmarket-only', action='store_true', help='Only scan for off-market emails')
    args = parser.parse_args()

    scan_gmail(
        days=args.days,
        proping_only=args.proping_only,
        offmarket_only=args.offmarket_only,
    )
