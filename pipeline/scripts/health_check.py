#!/usr/bin/env python3
"""
health_check.py — is the data feeding the app actually healthy?

Runs straight after the nightly pipeline (and the midday catch-up). Every
step of the pipeline "succeeds" even when a source has quietly broken, which
is how two problems went unnoticed in September 2026:
  - Proping changed its email layout on 8 Sep; bedrooms went blank for six
    days before anyone looked.
  - The For Sale inventory had not been refreshed from Domain since April.
This script checks what the run actually produced, not just whether it
exited cleanly, and emails a plain-English alert when something is wrong.

Emails (from mazarmartinapp@gmail.com, using the same Gmail app password the
pipeline already uses to read the inbox):
  - an ALERT when a problem first appears or changes, repeated every few days
    while it persists,
  - a RESOLVED note when a failure clears,
  - a routine SUMMARY every second Monday even when all is well (the
    fortnightly data-source audit agreed with Mazar Martin, Sep 2026).

Never fails the workflow: a broken health check must not block a deploy.

Usage:
  python health_check.py                 # check, and email if warranted
  python health_check.py --dry-run       # print the email instead of sending
  python health_check.py --summary       # force the routine summary email

Env:
  GMAIL_APP_PASSWORD   (or pipeline/scripts/.mm_credentials)
  HEALTH_ALERT_TO      comma-separated recipients (default below)
"""

import json
import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE = SCRIPT_DIR.parent
MASTER = PIPELINE / 'mazar_martin_app.html'
STATE = PIPELINE / 'health_state.json'
SENDER = 'mazarmartinapp@gmail.com'
DEFAULT_TO = 'david@tavoautomation.com'
APP_URL = 'https://app.mazarmartin.com.au/'

FAIL, WARN, OK = 'FAIL', 'WARN', 'OK'
REPEAT_ALERT_DAYS = 3        # re-send an unchanged FAIL this often
OFFMARKET_QUIET_DAYS = 21    # no forwarded off-market email for this long -> warn
FORSALE_STALE_DAYS = 30      # For Sale inventory older than this -> warn
# Checks that only a full nightly run performs (see main()).
NIGHTLY_ONLY_CHECKS = ('domain-', 'onthehouse-', 'agency-')


# ── helpers ─────────────────────────────────────────────────────────────────

def _load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return default


def _js_array(html, pattern):
    """Extract a JSON array assigned in the app HTML, e.g. `const x = [...]`."""
    m = re.search(pattern, html)
    if not m:
        return None
    start = m.end() - 1
    try:
        value, _ = json.JSONDecoder().raw_decode(html[start:])
        return value
    except Exception:
        return None


def _parse_date(s):
    s = str(s or '').strip()
    if not s:
        return None
    for candidate in (s, s[:10], s[:11]):
        for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d %b %Y', '%d %B %Y'):
            try:
                return datetime.strptime(candidate.strip(), fmt).date()
            except Exception:
                pass
    try:
        return datetime.fromisoformat(s[:19]).date()
    except Exception:
        return None


def _pct(n, d):
    return int(round(100 * n / d)) if d else 0


def _mtime_dt(path):
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime)
    except Exception:
        return None


# ── checks ──────────────────────────────────────────────────────────────────
# Each returns a list of (level, check_id, source, message, what_to_do).
# check_id is stable across days (no numbers) so "same problem as yesterday"
# can be recognised and not re-emailed every morning.

def check_run(last_run):
    out = []
    if not last_run:
        return [(WARN, 'run-record', 'Pipeline',
                 'No record of the last pipeline run was found.',
                 'Check the latest GitHub Actions run log.')]
    bad = [s for s in last_run.get('steps', []) if s.get('status') not in ('ok', 'skipped')]
    if bad:
        names = ', '.join(f"{s['label'].split(None, 1)[-1]} ({s['status']})" for s in bad)
        out.append((FAIL, 'steps-failed', 'Pipeline',
                    f'{len(bad)} step(s) failed: {names}.',
                    'Open the GitHub Actions log for that run and search for the step name.'))
    else:
        out.append((OK, 'steps-failed', 'Pipeline',
                    f"All steps completed ({last_run.get('mode', 'full')} run).", ''))
    return out


def check_scraper_outputs(last_run):
    """Did each website scraper actually write fresh output this run?"""
    out = []
    if not last_run or last_run.get('mode') != 'full':
        return out
    try:
        started = datetime.fromisoformat(last_run['started'])
    except Exception:
        return out
    for check_id, source, fname in (
        ('onthehouse-stale', 'OnTheHouse', 'onthehouse_listings.json'),
        ('agency-stale', 'Agency websites', 'agency_websites_listings.json'),
    ):
        path = PIPELINE / fname
        rows = _load_json(path, [])
        mt = _mtime_dt(path)
        if mt is None:
            out.append((WARN, check_id, source, 'Produced no output file.',
                        'The scraper may be failing to reach the website.'))
        elif mt < started - timedelta(minutes=5):
            out.append((WARN, check_id, source,
                        f'Did not refresh its data this run (last updated {mt:%d %b}).',
                        'The website may have changed or be blocking the scraper.'))
        elif not rows:
            out.append((WARN, check_id, source, 'Ran but found 0 properties.',
                        'The website may have changed its layout.'))
        else:
            out.append((OK, check_id, source, f'{len(rows)} properties.', ''))
    return out


def check_domain(last_run):
    out = []
    if last_run and last_run.get('mode') != 'full':
        return out
    st = _load_json(PIPELINE / 'domain_scrape_stats.json')
    if not st:
        return [(WARN, 'domain-nostats', 'Domain (Scrapfly)', 'No record of the Domain scrape.',
                 'Check the Domain step in the GitHub Actions log.')]
    pf = st.get('preflight') or {}
    if st.get('refused'):
        fmt = lambda v: f'{v:,}' if isinstance(v, int) else '?'
        return [(FAIL, 'domain-credits', 'Domain (Scrapfly)',
                 f"Skipped: not enough Scrapfly credits ({fmt(pf.get('credits_remaining'))} left, "
                 f"~{fmt(pf.get('credits_needed'))} needed).",
                 'Top up or upgrade the Scrapfly plan (Mazar Martin account).')]
    if st.get('fetcher') != 'scrapfly':
        out.append((FAIL, 'domain-noscrapfly', 'Domain (Scrapfly)',
                    'Domain was scraped without Scrapfly, which Domain blocks.',
                    'Check the SCRAPFLY_API_KEY secret in the GitHub repository.'))
    ok, denied = st.get('pages_ok', 0), st.get('pages_denied', 0)
    total = ok + denied
    if total and ok == 0:
        out.append((FAIL, 'domain-blocked', 'Domain (Scrapfly)',
                    f'All {total} pages failed or were blocked.',
                    'Scrapfly may be down, out of credits, or Domain has changed its protection.'))
    elif total and _pct(denied, total) > 20:
        out.append((WARN, 'domain-blocked', 'Domain (Scrapfly)',
                    f'{_pct(denied, total)}% of pages failed ({denied} of {total}).',
                    'Worth watching; if it rises, check Scrapfly status.'))
    else:
        out.append((OK, 'domain-blocked', 'Domain (Scrapfly)',
                    f"{ok} pages fetched, {st.get('credits_used', 0):,} credits used.", ''))
    left, need = pf.get('credits_remaining'), pf.get('credits_needed')
    if isinstance(left, int) and isinstance(need, int) and need and left < need * 3:
        out.append((WARN, 'domain-credits', 'Domain (Scrapfly)',
                    f'Only about {left // need} nightly run(s) of Scrapfly credit left ({left:,}).',
                    'Top up before it runs out, or the Domain step will be skipped.'))
    return out


def check_proping(history, today):
    out = []
    if not history:
        return [(FAIL, 'proping-missing', 'Proping emails', 'No Proping history found in the app.',
                 'Check the Gmail step in the GitHub Actions log.')]
    days = sorted(((_parse_date(d.get('date')), d) for d in history if _parse_date(d.get('date'))),
                  key=lambda x: x[0], reverse=True)
    secs = ('newly_listed', 'price_changes', 'sold', 'auction_changes', 'unlisted', 'over_90_days')
    with_data = [(dt, d) for dt, d in days if any(d.get(s) for s in secs)]
    if not with_data:
        return [(FAIL, 'proping-missing', 'Proping emails', 'Proping history is empty.', '')]
    latest = with_data[0][0]
    age = (today - latest).days
    if age > 3:
        out.append((FAIL, 'proping-age', 'Proping emails',
                    f'No Proping data since {latest:%a %d %b} ({age} days).',
                    'Check the Proping emails are still being forwarded to mazarmartinapp@gmail.com, '
                    'and that the Gmail app password still works.'))
    elif age > 1:
        out.append((WARN, 'proping-age', 'Proping emails',
                    f'Latest Proping data is from {latest:%a %d %b} ({age} days ago).',
                    'Usually just a quiet market or a late email; check if it persists.'))
    else:
        out.append((OK, 'proping-age', 'Proping emails', f'Latest data {latest:%a %d %b}.', ''))

    # Parsing quality over the most recent days that have listings. This is the
    # check that would have caught Proping's 8 Sep layout change on day one.
    sample, used = [], 0
    for dt, d in with_data:
        rows = [e for s in ('newly_listed', 'price_changes', 'sold') for e in d.get(s, [])]
        if rows:
            sample.extend(rows)
            used += 1
        if used >= 3 and len(sample) >= 10:
            break
    if len(sample) >= 10:
        n = len(sample)
        beds = sum(1 for e in sample if str(e.get('beds') or '').strip())
        price = sum(1 for e in sample if str(e.get('price') or '').strip())
        if _pct(beds, n) < 70:
            out.append((FAIL, 'proping-layout', 'Proping emails',
                        f'Bedrooms missing on {100 - _pct(beds, n)}% of recent Proping listings.',
                        'Proping has probably changed its email layout; the email parser needs updating.'))
        elif _pct(price, n) < 80:
            out.append((FAIL, 'proping-layout', 'Proping emails',
                        f'Prices missing on {100 - _pct(price, n)}% of recent Proping listings.',
                        'Proping has probably changed its email layout; the email parser needs updating.'))
        else:
            out.append((OK, 'proping-layout', 'Proping emails',
                        f'Details reading correctly ({_pct(beds, n)}% with bedrooms, {n} recent listings).', ''))
        pcs = [e for dt, d in with_data[:used] for e in d.get('price_changes', [])]
        if len(pcs) >= 5:
            amt = sum(1 for e in pcs if str(e.get('price_change') or '').strip())
            if _pct(amt, len(pcs)) < 60:
                out.append((WARN, 'proping-pricechange', 'Proping emails',
                            f'Price-change amounts missing on {100 - _pct(amt, len(pcs))}% of recent price changes.',
                            'The price-change parsing may need updating.'))
    return out


def check_offmarket(today):
    rows = _load_json(PIPELINE / 'offmarket_emails.json', [])
    dates = [d for d in (_parse_date(r.get('date')) for r in rows or []) if d]
    if not dates:
        return [(WARN, 'offmarket-quiet', 'Off-market emails',
                 'No off-market emails have ever been picked up from the app inbox.',
                 'Agent off-market emails need forwarding to mazarmartinapp@gmail.com.')]
    latest = max(dates)
    age = (today - latest).days
    if age > OFFMARKET_QUIET_DAYS:
        return [(WARN, 'offmarket-quiet', 'Off-market emails',
                 f'No off-market email picked up since {latest:%d %b} ({age} days).',
                 'Check the team is forwarding agent off-market emails to mazarmartinapp@gmail.com.')]
    return [(OK, 'offmarket-quiet', 'Off-market emails', f'Latest {latest:%d %b}.', '')]


def check_forsale(html, today):
    listings = _js_array(html, r'"sampleListings"\s*:\s*\[') or []
    dates = [d for d in (_parse_date(l.get('listDate')) for l in listings) if d]
    if not dates:
        return []
    newest = max(dates)
    age = (today - newest).days
    if age > FORSALE_STALE_DAYS:
        return [(WARN, 'forsale-stale', 'For Sale inventory',
                 f'The For Sale list was last refreshed from Domain on {newest:%d %b %Y} ({age} days).',
                 'Known issue: the Domain inventory loader is not yet part of the nightly run.')]
    return [(OK, 'forsale-stale', 'For Sale inventory', f'Newest listing {newest:%d %b}.', '')]


# ── email ───────────────────────────────────────────────────────────────────

def _password():
    pw = os.environ.get('GMAIL_APP_PASSWORD', '').strip()
    if pw:
        return pw
    creds = SCRIPT_DIR / '.mm_credentials'
    if creds.exists():
        for line in creds.read_text(encoding='utf-8').splitlines():
            if line.startswith('GMAIL_APP_PASSWORD='):
                return line.split('=', 1)[1].strip()
    return ''


def build_email(kind, results, last_run, resolved=()):
    problems = [r for r in results if r[0] != OK]
    fails = [r for r in problems if r[0] == FAIL]
    if kind == 'resolved':
        subject = 'Resolved: Mazar Martin app data sources are healthy again'
    elif kind == 'summary':
        subject = ('Fortnightly check: Mazar Martin app data sources healthy' if not problems else
                   f'Fortnightly check: {len(problems)} data issue(s) in the Mazar Martin app')
    else:
        subject = (f'Alert: {len(fails)} data problem(s) in the Mazar Martin app' if fails else
                   f'Heads-up: {len(problems)} data warning(s) in the Mazar Martin app')

    def row(r):
        level, _cid, source, msg, todo = r
        colour = {'FAIL': '#C62828', 'WARN': '#B45309', 'OK': '#2E7D32'}[level]
        label = {'FAIL': 'Problem', 'WARN': 'Warning', 'OK': 'OK'}[level]
        todo_html = f'<div style="color:#555;margin-top:2px">What to do: {todo}</div>' if todo and level != OK else ''
        return (f'<tr><td style="padding:8px 10px;vertical-align:top;white-space:nowrap;color:{colour};font-weight:700">{label}</td>'
                f'<td style="padding:8px 10px;vertical-align:top;font-weight:600">{source}</td>'
                f'<td style="padding:8px 10px;vertical-align:top">{msg}{todo_html}</td></tr>')

    ordered = sorted(results, key=lambda r: {'FAIL': 0, 'WARN': 1, 'OK': 2}[r[0]])
    intro = {
        'alert': 'The nightly data update finished, but something needs attention:',
        'resolved': 'The problems from the previous alert have cleared. Current status:',
        'summary': 'Routine fortnightly check of every data source feeding the app:',
    }[kind]
    run_line = ''
    if last_run:
        run_line = (f"Run: {last_run.get('mode', 'full')}, started {last_run.get('started', '?').replace('T', ' ')}, "
                    f"finished {last_run.get('finished', '?').replace('T', ' ')}.")
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:720px">
<p>{intro}</p>
<table style="border-collapse:collapse;width:100%;border:1px solid #ddd">{''.join(row(r) for r in ordered)}</table>
<p style="color:#777;font-size:12px;margin-top:14px">{run_line}<br>App: <a href="{APP_URL}">{APP_URL}</a><br>
Sent automatically by the Mazar Martin data pipeline.</p></div>"""
    text_lines = [intro, '']
    for level, _cid, source, msg, todo in ordered:
        text_lines.append(f'[{level}] {source}: {msg}' + (f'  -> {todo}' if todo and level != OK else ''))
    text_lines += ['', run_line, APP_URL]
    return subject, '\n'.join(text_lines), html


def send_email(subject, text, html, recipients, dry_run):
    if dry_run:
        print('\n' + '=' * 70 + f'\nDRY RUN — would email {", ".join(recipients)}\nSubject: {subject}\n' + '-' * 70)
        print(text)
        print('=' * 70)
        return True
    pw = _password()
    if not pw:
        print('  cannot send health email: no GMAIL_APP_PASSWORD')
        return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f'Mazar Martin App <{SENDER}>'
    msg['To'] = ', '.join(recipients)
    msg.attach(MIMEText(text, 'plain', 'utf-8'))
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=60) as s:
            s.starttls()
            s.login(SENDER, pw)
            s.sendmail(SENDER, recipients, msg.as_string())
        print(f'  health email sent to {", ".join(recipients)}: {subject}')
        return True
    except Exception as e:
        print(f'  health email FAILED to send: {e}')
        return False


# ── main ────────────────────────────────────────────────────────────────────

def main():
    dry_run = '--dry-run' in sys.argv
    force_summary = '--summary' in sys.argv
    recipients = [a.strip() for a in os.environ.get('HEALTH_ALERT_TO', DEFAULT_TO).split(',') if a.strip()]
    today = date.today()   # the workflow sets TZ=Australia/Sydney

    last_run = _load_json(PIPELINE / 'last_run.json')
    html = MASTER.read_text(encoding='utf-8') if MASTER.exists() else ''
    history = _js_array(html, r'const propingHistory\s*=\s*\[') if html else None

    results = []
    for fn, args in ((check_run, (last_run,)), (check_scraper_outputs, (last_run,)),
                     (check_domain, (last_run,)), (check_proping, (history, today)),
                     (check_offmarket, (today,)), (check_forsale, (html, today))):
        try:
            results.extend(fn(*args))
        except Exception as e:
            results.append((WARN, f'check-error-{fn.__name__}', 'Health check',
                            f'{fn.__name__} could not run: {e}', 'Look at health_check.py.'))

    print('\n========== Data health ==========')
    for level, _cid, source, msg, _todo in sorted(results, key=lambda r: {'FAIL': 0, 'WARN': 1, 'OK': 2}[r[0]]):
        print(f'  {level:<4}  {source:<20} {msg}')

    state = _load_json(STATE, {}) or {}
    prev_sig = state.get('signature', [])
    signature = sorted(f'{r[0]}:{r[1]}' for r in results if r[0] != OK)
    full_run = not last_run or last_run.get('mode') == 'full'
    if not full_run:
        # A midday email-only run doesn't re-check Domain or the website
        # scrapers. Carry their last known state forward, otherwise their
        # absence would look like "fixed" (a false RESOLVED email) and the next
        # night would re-alert.
        signature = sorted(set(signature) | {
            s for s in prev_sig if s.split(':', 1)[-1].startswith(NIGHTLY_ONLY_CHECKS)})
    had_fail = any(s.startswith('FAIL:') for s in prev_sig)
    has_fail = any(s.startswith('FAIL:') for s in signature)
    last_alert = _parse_date(state.get('last_alert'))
    last_summary = _parse_date(state.get('last_summary'))

    kind = None
    if signature and signature != prev_sig:
        kind = 'alert'                                   # new or changed problem
    elif has_fail and (not last_alert or (today - last_alert).days >= REPEAT_ALERT_DAYS):
        kind = 'alert'                                   # still failing: remind
    elif had_fail and not has_fail:
        kind = 'resolved'
    # Fortnightly summary comes from the nightly (full) run only, so it always
    # covers every source.
    is_summary_day = today.weekday() == 0 and today.isocalendar()[1] % 2 == 0
    if force_summary or (full_run and is_summary_day and last_summary != today and kind is None):
        kind = 'summary'

    if kind:
        subject, text, body = build_email(kind, results, last_run)
        sent = send_email(subject, text, body, recipients, dry_run)
        if sent and not dry_run:
            if kind in ('alert', 'resolved'):
                state['last_alert'] = today.isoformat()
            if kind == 'summary':
                state['last_summary'] = today.isoformat()
    else:
        print('  no email needed (no new problems; not a summary day)')

    if not dry_run:
        state['signature'] = signature
        state['last_check'] = datetime.now().isoformat(timespec='seconds')
        try:
            STATE.write_text(json.dumps(state, indent=2), encoding='utf-8')
        except Exception as e:
            print(f'  could not save health state: {e}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:           # never block the deploy
        print(f'health_check crashed: {e}')
    sys.exit(0)
