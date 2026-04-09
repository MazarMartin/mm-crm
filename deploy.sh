#!/bin/bash
# deploy.sh — commit + push the latest mazar_martin_app.html
#
# Run manually: ./deploy.sh
# Run from daily_update.py: bash /Users/gf/Downloads/mazar-martin-deploy/deploy.sh

set -e
cd "$(dirname "$0")"

# Stage all changes
git add -A

# Only commit if there are changes
if git diff --cached --quiet; then
    echo "[deploy] No changes to commit."
    exit 0
fi

TS=$(date '+%Y-%m-%d %H:%M')
git commit -m "Daily update — $TS" --quiet
echo "[deploy] Committed: Daily update — $TS"

# Push only if a remote is configured
if git remote get-url origin > /dev/null 2>&1; then
    echo "[deploy] Pushing to origin..."
    git push origin main --quiet 2>&1 || {
        echo "[deploy] WARNING: push failed (check credentials/remote)"
        exit 0
    }
    echo "[deploy] Pushed successfully."
else
    echo "[deploy] No remote configured. Run: git remote add origin <url>"
fi
