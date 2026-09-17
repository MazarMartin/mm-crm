#!/usr/bin/env python3
"""Run the full Mazar Martin scrape -> inject -> wash -> fill -> deploy pipeline.

Cross-platform replacement for Gerard's _run_scrape_wash_deploy.command. Each
step runs as its own subprocess (matching the original), reading the master
HTML that the previous step wrote. After the steps, the updated master is
copied to the repo-root index.html and pushed to the fork's main branch, which
GitHub Pages deploys.

Usage:
    python run_pipeline.py                 # full run incl. cp + git push
    python run_pipeline.py --no-deploy     # run steps only; skip cp + push
    python run_pipeline.py --skip-scrapers # skip the 4 scraper steps (offline)
    python run_pipeline.py --dry-run       # print the plan; run nothing
"""
import json
import os
import sys
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# Force UTF-8 so the scripts' emoji status output doesn't crash on a Windows
# console (cp1252). Set before spawning children so they inherit it.
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SCRIPTS = Path(__file__).resolve().parent       # pipeline/scripts
PIPELINE = SCRIPTS.parent                        # pipeline/  (master HTML + JSONs)
REPO_ROOT = PIPELINE.parent                      # repo root  (deploy target)
MASTER = PIPELINE / "mazar_martin_app.html"
INDEX = REPO_ROOT / "index.html"

# (label, filename) in the exact order Gerard's .command runs them.
STEPS = [
    ("0  scrape_gmail",             "scrape_gmail.py"),
    ("0b scrape_agency_websites",   "scrape_agency_websites.py"),
    ("1  scrape_onthehouse",        "scrape_onthehouse.py"),
    ("2  scrape_domain_realestate", "scrape_domain_realestate.py"),
    ("3  inject_email_data",        "inject_email_data.py"),
    ("4  wash_properties",          "wash_properties.py"),
    ("4b fill_forsale",             "fill_forsale.py"),
    ("4c fill_sold_prices",         "fill_sold_prices.py"),
    ("4d fill_sold_fields",         "fill_sold_fields.py"),
    ("4h fill_last_sold",           "fill_last_sold.py"),
    ("4e fill_proping_fields",      "fill_proping_fields.py"),
    ("4f inject_agency_offmarket",  "inject_agency_offmarket.py"),
    ("4g dedup_offmarket",          "dedup_offmarket.py"),
]

SCRAPERS = {
    "scrape_gmail.py",
    "scrape_agency_websites.py",
    "scrape_onthehouse.py",
    "scrape_domain_realestate.py",
}


def run_step(label, script, timings):
    """Run one step. Returns a failure description, or None on success.
    Never aborts the pipeline — matches the original .command, which runs
    every step regardless (web scrapers fail often and shouldn't kill the run).
    Records duration into `timings` for end-of-run summary."""
    path = SCRIPTS / script
    if not path.exists():
        print(f"  WARNING: skipping {label} - {script} not found")
        timings.append((label, 0.0, 'not found'))
        return f"{label} (not found)"
    started = datetime.now()
    print(f"\n--- Step {label} (started {started:%H:%M:%S}) ---", flush=True)
    result = subprocess.run([sys.executable, str(path)], cwd=str(SCRIPTS))
    elapsed = (datetime.now() - started).total_seconds()
    print(f"--- Step {label} finished in {elapsed:.1f}s ---", flush=True)
    if result.returncode != 0:
        print(f"\n  WARNING: {script} exited {result.returncode} - continuing")
        timings.append((label, elapsed, f'exit {result.returncode}'))
        return f"{label} (exit {result.returncode})"
    timings.append((label, elapsed, 'ok'))
    return None


def deploy():
    print("\n--- Step 5: Copy + Deploy ---", flush=True)
    shutil.copy(MASTER, INDEX)
    print(f"  Copied {MASTER.name} -> {INDEX}")

    def git(*args):
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args])

    git("add", "index.html")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("  No change to index.html - nothing to deploy.")
        return
    stamp = datetime.now().strftime("%d %b %Y %H:%M")
    if git("commit", "-m", f"Daily update {stamp}").returncode != 0:
        print("  FAILED: git commit")
        sys.exit(1)
    if git("push", "origin", "main").returncode != 0:
        print("  FAILED: git push")
        sys.exit(1)
    print("  Deployed: pushed index.html to origin/main (fork) -> GitHub Pages")


def main():
    skip_scrapers = "--skip-scrapers" in sys.argv
    # --email-only: re-read the inbox and rebuild from it, reusing the last
    # website scrapes. Used by the midday catch-up run when Proping sends late;
    # it must not re-run the Scrapfly/Domain step (credits) or slow scrapers.
    email_only = "--email-only" in sys.argv
    do_deploy = "--no-deploy" not in sys.argv
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("DRY RUN - the full run would execute, in order:")
        for label, script in STEPS:
            skipped = " (skipped: --skip-scrapers)" if skip_scrapers and script in SCRAPERS else ""
            print(f"  Step {label}{skipped}")
        print(f"  Step 5  deploy: cp master -> index.html + git push origin main"
              if do_deploy else "  Step 5  deploy: DISABLED (--no-deploy)")
        return

    if not MASTER.exists():
        print(f"ERROR: master HTML not found at {MASTER}")
        sys.exit(1)

    print(f"Pipeline start {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  master: {MASTER}")
    print(f"  deploy: {INDEX} (push to origin/main)" if do_deploy
          else "  deploy: DISABLED (--no-deploy)")

    failures = []
    timings = []
    pipeline_started = datetime.now()
    for label, script in STEPS:
        if skip_scrapers and script in SCRAPERS:
            print(f"\n--- Step {label}: SKIPPED (--skip-scrapers) ---")
            timings.append((label, 0.0, 'skipped'))
            continue
        if email_only and script in SCRAPERS and script != "scrape_gmail.py":
            print(f"\n--- Step {label}: SKIPPED (--email-only) ---")
            timings.append((label, 0.0, 'skipped'))
            continue
        problem = run_step(label, script, timings)
        if problem:
            failures.append(problem)

    if do_deploy:
        deploy()
    else:
        print("\n  (--no-deploy) Master HTML updated in place; cp + push skipped.")

    # Per-step timing summary — surfaces which step ate the time budget.
    total_elapsed = (datetime.now() - pipeline_started).total_seconds()
    print(f"\n========== Step timings (total {total_elapsed:.1f}s) ==========")
    for label, secs, status in sorted(timings, key=lambda r: -r[1]):
        bar = '#' * min(50, int(secs / max(1, total_elapsed) * 50))
        pct = (secs / total_elapsed * 100) if total_elapsed else 0
        print(f"  {label:<30} {secs:>7.1f}s  {pct:>5.1f}%  {status:<10} {bar}")

    if failures:
        print(f"\n  {len(failures)} step(s) had problems (run continued anyway):")
        for f in failures:
            print(f"    - {f}")

    # Machine-readable record of this run for health_check.py, which runs
    # straight after and reports failed steps / sources that didn't refresh.
    try:
        (PIPELINE / "last_run.json").write_text(json.dumps({
            "started": pipeline_started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"),
            "mode": "email-only" if email_only else ("skip-scrapers" if skip_scrapers else "full"),
            "steps": [{"label": l, "seconds": round(sec, 1), "status": st} for l, sec, st in timings],
            "failures": failures,
        }, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  (could not write last_run.json: {e})")

    print(f"\nDONE at {datetime.now():%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    main()
