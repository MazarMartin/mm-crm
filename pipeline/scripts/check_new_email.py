#!/usr/bin/env python3
"""
check_new_email.py — has anything new arrived since the last pipeline run?

Used by the midday catch-up workflow. Proping usually emails around 4:45am,
before the nightly run, but some days it sends late (e.g. 11:30am or 2pm).
Those emails used to wait until the next morning to reach the app. The
catch-up runs a couple of times a day; this script decides whether it needs
to do anything, so a quiet day costs only a minute of GitHub Actions time.

"New" = any email from a Mazar Martin address (Proping forwards and forwarded
off-market emails both come from staff mailboxes) that landed in the app
inbox after the last pipeline run finished.

Prints the result and, under GitHub Actions, writes `new_mail=true|false` to
$GITHUB_OUTPUT. Only needs the standard library, so it runs before
dependencies are installed.
"""

import email
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE = SCRIPT_DIR.parent
ADDRESS = 'mazarmartinapp@gmail.com'
STAFF_SENDER = re.compile(r'@mazarmartin\.com\.au|gerardmazar@', re.I)


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


def _emit(new_mail, reason):
    print(f'new_mail={str(new_mail).lower()}  ({reason})')
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as f:
            f.write(f'new_mail={str(new_mail).lower()}\n')


def main():
    try:
        last = json.loads((PIPELINE / 'last_run.json').read_text(encoding='utf-8'))
        # last_run times are local (TZ=Australia/Sydney on the runner)
        since = datetime.fromisoformat(last['finished']).astimezone()
    except Exception:
        return _emit(True, 'no record of a previous run')

    pw = _password()
    if not pw:
        return _emit(True, 'no Gmail password; running to be safe')

    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(ADDRESS, pw)
        mail.select('INBOX', readonly=True)          # never change read flags
        day = (since - timedelta(days=1)).strftime('%d-%b-%Y')
        _typ, data = mail.search(None, f'(SINCE {day})')
        ids = data[0].split()
        fresh = 0
        for mid in ids:
            _typ, md = mail.fetch(mid, '(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM)])')
            meta = b' '.join(x if isinstance(x, bytes) else x[0] for x in md if x)
            m = re.search(rb'INTERNALDATE "([^"]+)"', meta)
            if not m:
                continue
            received = datetime.strptime(m.group(1).decode(), '%d-%b-%Y %H:%M:%S %z')
            header = next((x[1] for x in md if isinstance(x, tuple)), b'')
            sender = email.message_from_bytes(header).get('From', '')
            if received > since and STAFF_SENDER.search(sender):
                fresh += 1
        mail.logout()
    except Exception as e:
        return _emit(True, f'could not check the inbox ({e}); running to be safe')

    if fresh:
        _emit(True, f'{fresh} new email(s) since the last run at {since:%d %b %H:%M}')
    else:
        _emit(False, f'nothing new since the last run at {since:%d %b %H:%M}')


if __name__ == '__main__':
    main()
