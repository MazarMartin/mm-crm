#!/usr/bin/env bash
# Assemble the public site for Cloudflare Pages.
#
# ALLOWLIST, not denylist: only the files named below are published. This
# is deliberate — the repo also holds the pipeline scrapers, the Supabase
# migrations/functions, internal docs, and client_report_prototype.html
# (an unused old prototype that still has real client data in it). None of
# that should be reachable from the web. Serving the repo root (the default)
# published all of it; this narrows it to just the front-end.
#
# If you add a NEW front-end file at the repo root, add its name to FILES
# below (or drop it in vendor/) or it won't appear on the live site.
set -e
rm -rf _site
mkdir -p _site

FILES=(
  index.html
  mm-supabase.js
  sw.js
  manifest.json
  icon-192.png
  icon-512.png
  icon-maskable-512.png
)
for f in "${FILES[@]}"; do
  [ -e "$f" ] && cp "$f" _site/
done

# Vendored front-end libraries (the Supabase JS client lives here).
[ -d vendor ] && cp -r vendor _site/

echo "Built _site with $(find _site -type f | wc -l) file(s):"
find _site -type f | sort
