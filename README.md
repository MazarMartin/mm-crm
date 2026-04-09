# Mazar Martin Buying Portal

Auto-deployed from the Mac mini each weekday morning by `daily_update.py`.

## How to add a remote (one-time setup)

To push automatically to GitHub Pages / Netlify / Vercel, create a private repo
and run:

```bash
cd /Users/gf/Downloads/mazar-martin-deploy
git remote add origin git@github.com:YOUR-USER/mazar-martin-deploy.git
git push -u origin main
```

After that, the daily script will push updates automatically each morning.

## Manual deploy

```bash
./deploy.sh
```
