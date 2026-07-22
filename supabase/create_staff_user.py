#!/usr/bin/env python3
"""Provision a STAFF login: Supabase auth user + org_members row + invite email.

Staff differ from clients (see create_client_user.py) in two ways:
  - they get an org_members row with role='staff', which is what
    fetchOrgId() looks for and what every RLS policy keys off;
  - they carry NO role/client_name metadata, so the frontend treats them
    as staff by default and they see the full app.

Idempotent: re-running for an existing email refreshes the membership and
re-sends a sign-in link rather than erroring.

Usage:
    pipeline/.venv/Scripts/python.exe supabase/create_staff_user.py \
        gerard@mazarmartin.com.au jeremy@mazarmartin.com.au

    # create accounts without emailing anyone yet:
    ... create_staff_user.py --no-email gerard@mazarmartin.com.au
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

SUPABASE_PROJECT_REF = "phkmwcimpyvmxbpdmuvw"
BASE_URL = f"https://{SUPABASE_PROJECT_REF}.supabase.co"
CREDS = Path(__file__).resolve().parent.parent / "pipeline" / "scripts" / ".mm_credentials"


def get_secret() -> str:
    if CREDS.exists():
        for line in CREDS.read_text(encoding="utf-8").splitlines():
            if line.startswith("SUPABASE_SECRET_KEY="):
                v = line.split("=", 1)[1].strip()
                if v:
                    return v
    sys.exit(f"ERROR: SUPABASE_SECRET_KEY not found in {CREDS}")


def api(method: str, path: str, secret: str, body=None, extra_headers=None):
    headers = {
        "apikey": secret,
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        sys.exit(f"ERROR: {method} {path} -> HTTP {e.code}: {detail}")


def find_user(email: str, secret: str):
    data = api("GET", "/auth/v1/admin/users?per_page=1000", secret)
    users = data.get("users") if isinstance(data, dict) else data
    for u in (users or []):
        if (u.get("email") or "").lower() == email.lower():
            return u
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("emails", nargs="+")
    ap.add_argument("--no-email", action="store_true",
                    help="Create the account but don't send a sign-in link")
    args = ap.parse_args()

    secret = get_secret()

    orgs = api("GET", "/rest/v1/orgs?select=id,name&limit=1", secret)
    if not orgs:
        sys.exit("ERROR: no org found")
    org_id, org_name = orgs[0]["id"], orgs[0].get("name", "?")
    print(f"Org: {org_name}\n")

    for email in args.emails:
        email = email.strip()
        existing = find_user(email, secret)

        if existing:
            user_id = existing["id"]
            print(f"• {email}: auth user already exists")
        else:
            created = api("POST", "/auth/v1/admin/users", secret,
                          body={"email": email, "email_confirm": True})
            user_id = created["id"]
            print(f"• {email}: auth user created")

        # Staff membership — this is what fetchOrgId() and every RLS
        # policy key off. Upsert so re-runs are safe.
        api("POST", "/rest/v1/org_members?on_conflict=org_id,user_id", secret,
            body={"org_id": org_id, "user_id": user_id, "role": "staff"},
            extra_headers={"Prefer": "resolution=merge-duplicates"})
        print(f"    staff membership ok")

        if not args.no_email:
            api("POST", "/auth/v1/magiclink", secret,
                body={"email": email, "create_user": False})
            print(f"    ✉️  sign-in link sent")

    print("\nDone. They click the emailed link and land in the full staff app.")


if __name__ == "__main__":
    main()
