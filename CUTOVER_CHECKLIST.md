# Mazar Martin — Cutover Checklist

The sequence to move from "Stage 2 sandbox on David's fork" to
"production on MazarMartin/mm-crm with Supabase". Read top to bottom.
Each numbered step has a clear "done when" so we know we're not skipping bits.

---

## Phase 0 — Pre-cutover (any time before the window)

**0.1 Confirm everything in `supabase/` runs cleanly**
```
pipeline/.venv/Scripts/python.exe supabase/apply.py
```
Should report 13 tables + Mazar Martin org + the staff member. Re-runnable.

**0.2 Pick the cutover window with Gerard**
- Low-activity time (evening or weekend).
- Gerard available for a 10–15 min screen-share to capture his localStorage.
- He should NOT make new edits during the window (otherwise they'll get
  stranded on his browser).

**0.3 Brief Gerard**
- One-paragraph explanation: "We're moving the app's data into a cloud database
  so edits sync across devices. From the cutover onwards, you and the team log
  in once with the shared credentials and edits go to the cloud automatically."
- Tell him: stop manually running the `.command` after cutover — the new system
  scrapes daily on its own.

---

## Phase 1 — Capture Gerard's current state

**1.1 Fresh whiteboard export**
- Gerard exports the whiteboard data from his app's Safari (he knows the workflow).
- He drops the resulting `whiteboard_data.json` into Drive.
- Pull it down to your machine, save somewhere obvious (e.g. `~/Downloads/gerard-whiteboard.json`).
- Done when: file exists, opens cleanly, has the expected sections (`activeBuyers`, `pipeline`, etc.).

**1.2 localStorage dump (DevTools snippet)**
- Walk Gerard through the dump (instructions below — also stored in plain English
  separately).
- Done when: a `gerard-localstorage.json` file is on your machine, starts with `{"mm...`,
  has more than just a few bytes.

---

## Phase 2 — Import into Supabase

**2.1 Wipe + re-import the whiteboard**
```
pipeline/.venv/Scripts/python.exe supabase/migrate_data.py --whiteboard ~/Downloads/gerard-whiteboard.json
```
Output should list the section breakdown (Pipeline N / Active Buyer M / etc.).

**2.2 Import the localStorage dump (with --wipe so Gerard's data is canonical)**
```
pipeline/.venv/Scripts/python.exe supabase/import_localstorage.py ~/Downloads/gerard-localstorage.json --wipe
```
Output lists imported counts per key + any skipped/unknown.

**2.3 Spot-check in Supabase**
- Table Editor → `clients`: count looks right, names match the whiteboard.
- Table Editor → `agent_calls`: rows for agents Gerard's been calling.
- Table Editor → `client_comments` / `saved_matches`: rows where expected.
- If anything looks off, fix the dump or re-run with adjustments before moving on.

---

## Phase 3 — Transplant the integration block to MazarMartin/mm-crm

**3.1 Open a PR from your fork → upstream**
- On GitHub: go to `DavidLEPacheco/mm-crm` → "Pull requests" → "New pull request" →
  set base = `MazarMartin/mm-crm:main`, compare = `DavidLEPacheco/mm-crm:main`.
- The PR will include all our Stage 1 + Stage 2 work. Confirm it includes:
  - `mm-supabase.js`
  - `.github/workflows/pipeline.yml` (Stage 1 cron pipeline)
  - `supabase/` folder (schema + scripts + migrations)
  - `pipeline/` folder (cross-platform Python pipeline)
  - The `<script type="module" src="./mm-supabase.js"></script>` tag in `index.html`
  - `requirements.txt` deps, `.gitignore` updates
- It will also include a lot of `index.html` churn (daily updates from the
  fork's pipeline) — that's fine; merging fast-forwards the upstream too.

**3.2 Coordinate the merge with Gerard**
- Just before merging: confirm with Gerard that he won't push for the next hour.
- Merge the PR. (Or have him merge.)

**3.3 Add secrets to MazarMartin/mm-crm**
- Settings → Secrets and variables → Actions → New repository secret:
  - `GMAIL_APP_PASSWORD` = the same one we used on the fork.
- That's the only secret the workflow needs.

**3.4 Confirm GitHub Pages is set to "deploy via GitHub Actions"**
- In MazarMartin/mm-crm → Settings → Pages → Source: "GitHub Actions".
- If it's currently "Deploy from a branch", switch it (one-time).

---

## Phase 4 — Verify the live site

**4.1 Trigger the pipeline once manually**
- MazarMartin/mm-crm → Actions → "Daily pipeline (scrape + deploy)" → "Run workflow".
- Watch it complete green (~15–20 min if the scrapers fully run).

**4.2 Open the live site (incognito window)**
- https://mazarmartin.github.io/mm-crm/
- You should see the dark-green Mazar Martin login overlay.
- Sign in with `mazarmartinapp@gmail.com` + the shared password.
- Overlay should say `✅ Loaded N keys — opening app…` with N > 0.
- Confirm clients, comments, call statuses appear as expected.

**4.3 Have Gerard sign in from his Mac**
- Same login. The app should look identical to before, with everything intact.
- Have him add/edit one tiny thing to confirm writes work (e.g., toggle one
  client's section). Check it appears in Supabase Table Editor.

---

## Phase 5 — Decommission the old workflow

**5.1 Stop Gerard running `.command` manually**
- Tell him explicitly: "From now on, don't run `_run_scrape_wash_deploy.command`.
  It'll fight the new cloud pipeline and overwrite changes."

**5.2 Unload his LaunchAgent**
- On his Mac, Terminal:
  ```
  launchctl unload ~/Library/LaunchAgents/com.mazarmartin.daily-scrape.plist
  ```
- (And any other variants — the chat log mentioned several stale ones.)

**5.3 Keep his local scripts as backup for a week**
- Don't delete his Mac's `~/Downloads/lns_agents_scripts/` immediately.
- Sanity buffer: if the new system has an issue, he can manually run the old
  one as a fallback. Plan to delete after 7 days of green new-pipeline runs.

---

## Phase 6 — Aftercare

**6.1 Monitor**
- Each morning for the first week: check the new pipeline's Actions run.
- Verify the daily commit appears on Gerard's main branch with a recent timestamp.

**6.2 Update PROJECT_LOG.md**
- Add a cutover-day entry summarizing what landed and any surprises.

**6.3 Bill the work**
- Per the engagement plan, this completes Stage 2.

---

## Gerard's localStorage dump — plain-English instructions

(For walking Gerard through on a screen-share, or emailing him to do solo.)

### Step 1 — Make sure the "Develop" menu exists in Safari (one-time only)

1. Look at the very top of the screen, next to the Apple logo. You should see
   menus: **Safari**, **File**, **Edit**, **View**, **History**, etc.
2. Is **Develop** one of those menus? (Should appear between Bookmarks/View and Window.)
3. If YES → skip to Step 2.
4. If NO → enable it:
   - Click **Safari** → **Settings…** (or "Preferences…" on older macOS).
   - Click the **Advanced** tab (gear icon).
   - At the bottom, tick **"Show features for web developers"**
     (sometimes labelled "Show Develop menu in menu bar").
   - Close that window. The **Develop** menu now appears in the menu bar.

### Step 2 — Open the Web Inspector on the Mazar Martin app

1. In Safari, open the Mazar Martin app: **mazarmartin.github.io/mm-crm/**
2. Wait for it to fully load (dashboard should appear).
3. In the menu bar, click **Develop** → **Show Web Inspector**.
   A panel opens at the bottom or side of the browser window.
4. At the top of that panel, find the tabs: Elements, Network, Sources, Storage, **Console**, etc.
   Click **Console**.

### Step 3 — Run the magic line

1. At the very bottom of the Console panel is a text box with a `>` symbol on the left.
   Click into that text box.
2. Copy this whole line exactly:
   ```
   copy(Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])))
   ```
3. Paste it into the text box and press **Return**.
4. The console may say `undefined` — that's normal, ignore it.
5. The data is now on the clipboard.

### Step 4 — Save it to a file

1. Open **TextEdit** (Applications → TextEdit, or Cmd+Space → type "TextEdit").
2. In TextEdit's menu bar, click **Format** → **Make Plain Text**.
   (If you see "Make Rich Text" instead, you're already in plain text — leave it.)
3. Click into the empty document and press **Cmd+V** to paste.
4. You should see a long string of text starting with `{` and full of `"mm…"` words.
5. **File → Save…**
   - Name: `gerard-localstorage.json` (the `.json` ending matters)
   - Where: Desktop (or any spot you'll remember)
   - Click **Save**.

### Step 5 — Send it to David

Email it, AirDrop it, drop in Drive, whatever's easiest. Done.

### If something goes wrong

- **"Develop" menu won't appear** → re-do Step 1; make sure you closed and re-opened
  the Settings window after ticking the box.
- **The pasted text in TextEdit is just `{}`** → the browser's localStorage is empty
  (unusual for Gerard — his should have lots). Make sure you're signed in to the
  app and the dashboard has loaded fully before running the line.
- **Pasting shows weird characters or seems cut off** → don't worry, JSON is one
  long line by design; it's fine.
- **Anything else** → screen-share with David; he'll do it for you.

---

# Hosting cutover — GitHub Pages → Cloudflare Pages (2026-09)

Why: GitHub Pages (free tier) requires a PUBLIC repo, which exposed the
scrapers, migrations, git history (incl. old client data) and let anyone
clone the whole system. Cloudflare Pages serves from a PRIVATE repo for
free. Runs fully in parallel until the final step, so nothing is at risk.

## Already done (2026-09-13)
- [x] Cloudflare account under the Mazar Martin email; Pages project `mm-crm`
      connected to `MazarMartin/mm-crm` via the MazarMartin GitHub account
      (Gerard granted access). No dependency on David's personal accounts.
- [x] Build: preset None · command `bash build.sh` · output `_site`.
      build.sh is an ALLOWLIST — only the 8 front-end files publish; pipeline,
      supabase/, docs and client_report_prototype.html are unreachable.
- [x] Parallel URL live and verified: https://mm-crm.pages.dev
      (app loads, assets resolve at root, magic-link login works).
- [x] Supabase → Auth → Redirect URLs includes `https://mm-crm.pages.dev/**`
      (GitHub URL kept; both work at once).

- [x] Custom domain `app.mazarmartin.com.au` added in Cloudflare (CNAME
      setup, NOT a DNS transfer: their DNS is GoDaddy / domaincontrol.com
      and email is Microsoft 365, both untouched). GoDaddy record:
      `app` CNAME -> `mm-crm.pages.dev`. Loads over HTTPS.
- [x] Supabase Redirect URLs includes `https://app.mazarmartin.com.au/**`.
      Magic link requested from the new domain lands back on it.

- [x] 2026-09-13 20:40 — Supabase Site URL switched to
      `https://app.mazarmartin.com.au`. Verified with an admin
      generate_link (unsent): redirect_to is the new domain.

## Cutover (remaining — ~10 min, any quiet evening)
1. ~~**Supabase Site URL**~~ DONE. (Kept for the record — it had to wait
   until the announcement was ready because the
   "Send login invite" button, `create_client_user.py` and
   `create_staff_user.py` pass no redirect, so every invite email uses the
   Site URL. Changing it early sends people to a domain nobody announced.
   Also why a link requested from a non-allowlisted domain "jumps" to
   GitHub: Supabase falls back to Site URL (and Chrome opens it in the
   installed GitHub PWA window if one exists).
2. **Tell people** — Gerard, Jeremy, Mon + client logins: new address, one
   fresh login (sessions are per-domain). Anyone who installed the GitHub
   version as a desktop app should uninstall it and reinstall from the new
   address. Old link keeps working until step 4.
3. **Workflows** — delete `.github/workflows/deploy.yml`; in `pipeline.yml`
   drop `configure-pages` / `upload-pages-artifact` / `deploy-pages`, the
   `pages:` + `id-token:` permissions and the `environment` block. Daily job
   just commits; Cloudflare builds on push. (David — code change.)
4. **Repo private** — MazarMartin/mm-crm -> Settings -> Change visibility ->
   Private (and David's fork). GitHub Pages stops (intended); Cloudflare
   keeps building. Done when mazarmartin.github.io/mm-crm/ is 404 and the
   new domain still updates after the next nightly run.
5. **Verify next morning** — Actions green, Cloudflare deployment green,
   yesterday's Proping data visible on app.mazarmartin.com.au.
6. **Tidy** — remove the GitHub URL from Supabase Redirect URLs once nobody
   is using it.

## Rollback (any point before step 4)
Nothing to undo — GitHub Pages is still serving. Just don't announce the
new URL. After step 4: flip the repo back to Public and Pages resumes.

## Not fixed by this (be straight if asked)
Anything already cloned while the repo was public, and the git history
itself (old client data in early commits) — a private repo stops NEW
access; it doesn't retrieve old copies. Scrubbing history is a separate
decision.
