# Freelancermap Monitor

A local Python monitor for Freelancermap project listings. It discovers projects, opens each new detail page, stores structured fields plus the original HTML in SQLite, deduplicates by canonical project URL/slug, and sends one HTML SMTP digest containing only newly discovered projects.

## What is stored

Each project record includes:

- Local numeric ID, canonical URL, source slug/key
- Title, company/provider, provider URL, contact person
- Location, city, country, workplace/remote percentage
- Contract type, duration, start date, publication data, validity date
- Industry, skills, rate, full description and description HTML
- Application URL, active/closed state
- Listing-card text and raw page metadata/JSON-LD
- Gzip-compressed raw detail-page HTML (enabled by default)
- First/last seen times, detail fetch status/errors, baseline/email state and attempts

The database also stores scan history and failures.

## Safety and access behavior

- Uses a normal Chrome browser through Selenium.
- Does not bypass CAPTCHA, MFA, rate limits, authentication, or access controls.
- Uses a persistent Chrome profile only to retain a login that you complete normally.
- Scans conservatively with configurable delays.
- It does not submit applications, message clients, or modify your Freelancermap account.

Review Freelancermap's current terms and use a scan frequency permitted for your account and jurisdiction.

## Windows setup

Open PowerShell in this folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
notepad .env
```

Fill in SMTP values in `.env`. For Google Workspace/Gmail, use an App Password rather than the normal account password when two-step verification is enabled.

## Recommended first run

```powershell
.\.venv\Scripts\Activate.ps1
python main.py --test-browser --visible
python main.py --send-test-email
python main.py --initialize-baseline
python main.py --db-status
python main.py --run-once
```

`AUTO_BASELINE_ON_FIRST_RUN=true` is enabled by default. Therefore, even when you skip the explicit baseline command, the first empty-database scan stores existing projects without emailing them. Future projects are emailed.

## Login

Project listings and most project details are public, so login is not required for the normal monitor. To retain your authenticated account session:

```powershell
python main.py --interactive-login
python main.py --test-login --visible
```

A Chrome window opens. Complete login and any CAPTCHA/MFA yourself. The session is retained under `data/chrome_profile`.

Credential-based login is also supported, but interactive login is more reliable:

```powershell
python main.py --login-with-credentials
```

## Normal operation

One cycle:

```powershell
python main.py --run-once
```

Continuous scan every 600 seconds (default):

```powershell
python main.py
```

Or run `run_monitor.ps1`.

## Useful commands

```powershell
python main.py --dry-run --run-once
python main.py --db-status
python main.py --list-projects --limit 30
python main.py --export-csv data\freelancermap_projects.csv
python main.py --retry-failed-details
python main.py --test-browser --visible
python main.py --send-test-email
```

## Main configuration

- `FREELANCERMAP_PROJECTS_URL`: set a filtered/search URL to monitor only relevant projects.
- `CHECK_INTERVAL_SECONDS=600`: cycle interval; minimum accepted by the app is 60 seconds, but a conservative interval is recommended.
- `MAX_PAGES=1`: scans the first listing page by default.
- `MAX_SCROLLS_PER_PAGE=3`: loads additional cards if the site uses lazy loading.
- `MAX_PROJECTS_PER_CYCLE=40`: maximum discovered cards processed per cycle.
- `MAX_DETAIL_PAGES_PER_CYCLE=30`: maximum detail requests per cycle.
- `DETAIL_MAX_ATTEMPTS=5`: stops repeatedly retrying a permanently failing detail page until manually reset.
- `REQUEST_DELAY_MIN_SECONDS=4` and `REQUEST_DELAY_MAX_SECONDS=8`: randomized polite delay between detail pages.
- `STORE_RAW_HTML=true`: stores compressed original HTML in SQLite.
- `AUTO_BASELINE_ON_FIRST_RUN=true`: prevents an initial flood of alerts.

## Files created at runtime

- `data/freelancermap_projects.db`
- `data/freelancermap_monitor.log`
- `data/chrome_profile/`

## Run tests

```powershell
python -m unittest discover -s tests -v
```

## Troubleshooting

**Chrome profile is already in use**  
Close Chrome windows opened by this monitor, then retry. The monitor uses its own `data/chrome_profile` directory.

**No project links found**  
Run `python main.py --test-browser --visible`. Check whether a cookie prompt, login page, network block, or site redesign is visible. The parser discovers canonical `/project/<slug>` links and avoids dependence on a single CSS class.

**SMTP authentication fails**  
Confirm host/port/TLS values. For Google Workspace/Gmail, use an App Password and ensure the `SMTP_USERNAME` is the full mailbox address.

**First run sent nothing**  
That is intentional when automatic baseline initialization is enabled. Run another cycle after a new project appears.
