#!/usr/bin/env python3
"""Provision a client login: clients-table row + Supabase auth user + invite email.

Does three things, all idempotent:
  1. Upserts a row in the `clients` table (org-scoped, name-keyed) with the
     supplied preferences in the `edits` JSONB — this is what makes the
     client appear in the app and gives their "My File" view its data.
  2. Creates the Supabase auth user with user_metadata
     {role: 'client', client_name: <name>} — the metadata the frontend
     reads to decide role and which client record to show.
  3. Sends the login email through the project's configured SMTP:
     - new user  → invite email (GoTrue /auth/v1/invite)
     - existing  → magic-link email (GoTrue /auth/v1/magiclink), metadata
       refreshed first so a re-run also repairs a bad role/client_name.

Uses SUPABASE_SECRET_KEY from pipeline/scripts/.mm_credentials (same
pattern as set_user_password.py). Never prints the key.

Usage:
    pipeline/.venv/Scripts/python.exe supabase/create_client_user.py \
        --email david.l.pacheco@outlook.com \
        --name "David Pacheco" \
        --budget "Up to $3.5m" \
        --locations "Mosman, Cremorne, Neutral Bay" \
        --beds 3 \
        --spec "3 bed house or townhouse, parking essential"

    # auth-only (client row already exists):
    ... create_client_user.py --email x@y.com --name "Jaidene and Jamie Simon" --no-seed
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


def read_creds_line(prefix: str) -> str:
    if not CREDS.exists():
        return ""
    for line in CREDS.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()
    return ""


def get_secret() -> str:
    secret = read_creds_line("SUPABASE_SECRET_KEY=")
    if not secret:
        sys.exit(f"ERROR: SUPABASE_SECRET_KEY not found in {CREDS}")
    return secret


def api(method: str, path: str, secret: str, body: dict | None = None,
        extra_headers: dict | None = None):
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
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True, help="Client name exactly as it appears in the app")
    ap.add_argument("--budget", default="")
    ap.add_argument("--locations", default="", help="Comma-separated suburbs")
    ap.add_argument("--beds", default="")
    ap.add_argument("--baths", default="")
    ap.add_argument("--car", default="")
    ap.add_argument("--spec", default="")
    ap.add_argument("--section", default="Active Buyer")
    ap.add_argument("--no-seed", action="store_true",
                    help="Skip the clients-table upsert (auth user + email only)")
    ap.add_argument("--no-email", action="store_true",
                    help="Create/update the auth user but don't send any email")
    args = ap.parse_args()

    secret = get_secret()

    # ── 1. clients table row ────────────────────────────────────────────
    if not args.no_seed:
        orgs = api("GET", "/rest/v1/orgs?select=id&limit=1", secret)
        if not orgs:
            sys.exit("ERROR: no org found")
        org_id = orgs[0]["id"]

        edits = {k: v for k, v in {
            "section":   args.section,
            "budget":    args.budget,
            "locations": args.locations,
            "prefBeds":  args.beds,
            "prefBaths": args.baths,
            "prefCar":   args.car,
            "spec":      args.spec,
            "email":     args.email,
        }.items() if v}

        # Upsert on the (org_id, name) unique constraint from migration 0002.
        api("POST", "/rest/v1/clients?on_conflict=org_id,name", secret,
            body={"org_id": org_id, "name": args.name,
                  "section": args.section, "edits": edits},
            extra_headers={"Prefer": "resolution=merge-duplicates"})
        print(f"✅ clients row upserted for '{args.name}'")

    # ── 2 + 3. auth user + email ────────────────────────────────────────
    meta = {"role": "client", "client_name": args.name}
    existing = find_user(args.email, secret)

    if existing:
        api("PUT", f"/auth/v1/admin/users/{existing['id']}", secret,
            body={"user_metadata": meta})
        print(f"✅ auth user already existed — metadata refreshed ({args.email})")
        if not args.no_email:
            api("POST", "/auth/v1/magiclink", secret,
                body={"email": args.email, "create_user": False})
            print("✉️  magic-link email sent")
    else:
        if args.no_email:
            api("POST", "/auth/v1/admin/users", secret,
                body={"email": args.email, "email_confirm": True,
                      "user_metadata": meta})
            print(f"✅ auth user created, no email sent ({args.email})")
        else:
            api("POST", "/auth/v1/invite", secret,
                body={"email": args.email, "data": meta})
            print(f"✅ auth user created + invite email sent ({args.email})")

    print("\nDone. The client clicks the emailed link and lands in the app "
          "with role=client scoped to their record.")


if __name__ == "__main__":
    main()
