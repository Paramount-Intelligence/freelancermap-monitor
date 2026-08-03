# Freelancermap Monitor

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/Paramount-Intelligence/freelancermap-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/Paramount-Intelligence/freelancermap-monitor/actions/workflows/ci.yml)
[![tests: 299 passed](https://img.shields.io/badge/tests-299%20passed-brightgreen.svg)]()
[![Code Architecture](https://img.shields.io/badge/architecture-modular-orange.svg)]()

A robust, local Python monitoring tool for **Freelancermap** project listings. It automatically discovers new projects from one or two search feeds, extracts detailed assignment parameters using deep DOM and JSON-LD parsing, persists structured data alongside compressed raw HTML in SQLite, and delivers HTML email digests for newly published projects.

---

## Key Features

- **Automated Listing & Detail Discovery**: Periodically scans listing pages using Selenium WebDriver to capture dynamic cards, modal overlays, and React JSON state.
- **Resilient Parsing Engine**: Resiliently extracts title, company, location, workload, rate, duration, start date, contract type, workplace model, skills, and full project descriptions.
- **Preserves Critical Qualifiers**: Retains contract and rate qualifiers (e.g. `"6 months initial contract"`, `"€500/day (Outside IR35)"`) without premature truncation.
- **ACID-Compliant SQLite Storage (Schema 10)**: Atomic upserts deduplicate listings by canonical URL and `source_key` while maintaining full snapshot and observation histories, dual-feed provenance, and per-scan feed-status records.
- **Dual-Feed Discovery**: Optionally scans a second (personalized/relevant) feed; projects are merged by canonical URL, enriched from whichever feed carries richer data, and their provenance (`seen_in_primary`, `seen_in_personalized`, positions, `discovery_sources_json`) is persisted per row.
- **Authenticated Sessions**: Persistent Chrome profile keeps you logged in; the monitor verifies authentication through positive DOM markers (logout control / user menu), never by URL alone.
- **HTML Email Digest**: Sends styled HTML email digests via SMTP (`SMTP_SSL` or `STARTTLS`) with XSS protection and strict transaction journaling.
- **Resilient Recovery**: Resumes interrupted scans safely, retries failed detail fetches with bounded backoff, and ensures emails are marked sent **only after** SMTP server acceptance.
- **Conservative & Polite Scanning**: Respects target servers with configurable polite delays and a persistent Chrome profile session.
- **First-Run Safety Gate**: An empty database never emails a flood of existing projects; the first scan refuses to run until you explicitly create a baseline.

---

## Architecture Overview

```mermaid
flowchart TD
    A[Freelancermap Website] -->|Selenium / Chrome| B(BrowserSession)
    B -->|Raw Listing & Detail HTML| C(Parser Engine)
    C -->|ProjectDiscovery & Detail|     D[(SQLite Database v10)]
    D -->|New Pending Projects| E(Emailer Engine)
    E -->|SMTP / TLS| F[Email Recipients]
```

- **Browser Layer (`browser.py`)**: Manages Selenium Chrome instances, persistent session profiles, cookie consent popups, verified newest-first sorting, config-driven scrolling, bounded load-more clicks, and readiness checks.
- **Parsing Layer (`parser.py`)**: Uses BeautifulSoup and JSON-LD extractors to structure unstructured project facts with fallback chain resolution.
- **Database Layer (`database.py`)**: Handles SQLite schema migrations, foreign keys, WAL journaling, gzip HTML compression, provenance tracking, and CSV exports.
- **Alerting Layer (`emailer.py`)**: Formats multipart plain-text and HTML email digests with recipient verification and Message-ID tracking.
- **Orchestration Layer (`monitor.py` & `main.py`)**: Executes single-process cycles protected by non-blocking file locks.

---

## Dual-Feed Configuration

Freelancermap offers two sort modes, verified against the live site:

| `sort` value | Meaning |
| :--- | :--- |
| `sort=1` | **Newest projects first** |
| `sort=2` | **Relevant first** |

- `FREELANCERMAP_PRIMARY_SEARCH_URL` — the newest-first feed (the monitor **refuses to run** unless this URL contains `sort=1`). The monitor also verifies the rendered sort state in the DOM before scanning and rejects a page that is not sorted newest-first.
- `FREELANCERMAP_PERSONALIZED_SEARCH_URL` — an optional second feed (`sort=1` or `sort=2`). `ENABLE_PERSONALIZED_FEED=true` enables scanning it; projects already known from the primary feed are enriched with any richer data from this feed.

Example:

```env
FREELANCERMAP_PRIMARY_SEARCH_URL=https://www.freelancermap.com/projects?excludeDachProjects=false&query=website+development&sort=1&pagenr=1
FREELANCERMAP_PERSONALIZED_SEARCH_URL=https://www.freelancermap.com/projects?excludeDachProjects=false&query=automation&sort=2&pagenr=1
ENABLE_PERSONALIZED_FEED=true
PERSONALIZED_FEED_DISCOVERY=false
```

`PERSONALIZED_FEED_DISCOVERY` controls whether projects found **only** in the secondary feed are stored at all: `false` (default) uses the secondary feed purely for enrichment of primary-feed projects; `true` also inserts secondary-only projects.

Configuration validation (`python main.py --health-check`) rejects feed URLs that are missing the required sort parameter or point at login, account, dashboard, app, or project-detail routes.

---

## Extracted & Persisted Schema

Every project record captures the following structured fields:

| Field Name | Description | Example |
| :--- | :--- | :--- |
| **`title`** | Project title | `"Senior Python & DevOps Engineer"` |
| **`company`** | Hiring provider or client | `"Darwin Recruitment"` |
| **`location`** | Formatted location (City, Country) | `"Amsterdam, Netherlands"` |
| **`workplace`** | Attendance model | `"On-site"`, `"Remote"`, `"Hybrid"` |
| **`contract_type`** | Engagement classification | `"Freelance"`, `"Contract"` |
| **`duration`** | Preserved project duration | `"6 months (extension possible)"` |
| **`start_date`** | Planned start date | `"ASAP"`, `"09/2026"` |
| **`rate`** | Preserved compensation rate | `"€85 - €95 / hour"` |
| **`workload`** | Expected capacity | `"Full-time"`, `"80%"` |
| **`posted_at`** | Verified posting UTC timestamp | `"2026-07-30T23:38:00+00:00"` |

Feed provenance (schema 10):

| Field Name | Description |
| :--- | :--- |
| **`seen_in_primary`** | Row was seen in the primary (newest-first) feed |
| **`seen_in_personalized`** | Row was seen in the secondary feed |
| **`primary_position`** | First-seen position in the primary feed |
| **`personalized_position`** | First-seen position in the secondary feed |
| **`discovery_sources_json`** | Accumulated source labels (e.g. `["primary_newest", "personalized_relevant"]`) |

Every scan record additionally persists its **feed status**:

| Field Name | Description |
| :--- | :--- |
| **`primary_feed_status`** | Outcome of the primary feed load (`ok`, `failed`, `empty`, or `not_configured`) |
| **`personalized_feed_status`** | Outcome of the secondary feed load (`ok`, `failed`, `empty`, `skipped`, or `not_configured`) |
| **`degraded`** | Flag: the cycle completed while one feed was unavailable |
| **`degraded_reason`** | Human-readable explanation for the degraded state |
| **`primary_count`** | Number of project cards parsed from the primary feed |
| **`personalized_count`** | Number of project cards parsed from the secondary feed |
| **`personalized_only_count`** | Cards seen only in the secondary feed |
| **`ignored_personalized_only_count`** | Secondary-only cards not stored because `PERSONALIZED_FEED_DISCOVERY=false` |

---

## Quick Start & Windows Setup

### 1. Prerequisites
- **Python 3.10+**
- **Google Chrome** (for Selenium WebDriver)

### 2. Environment Installation (PowerShell)
```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

### 3. Environment Configuration
Copy `.env.example` to `.env` and fill in your SMTP details and feed URLs (see "Dual-Feed Configuration" above).

### 4. First Run (mandatory order)

```powershell
# 1. Log in once in a visible browser; the session is saved under data/chrome_profile
python main.py --interactive-login --visible

# 2. Create the baseline: stores existing projects WITHOUT emailing them
python main.py --initialize-baseline --visible
```

Without step 2, the first unattended run **refuses to scan**:

```
RuntimeError: Baseline is not initialized. Run: python main.py --initialize-baseline --visible
```

### 5. Running the Monitor

```powershell
python main.py                                # continuous monitor (600s default interval)
python main.py --run-once                     # one single cycle
python main.py --run-once --visible           # one cycle with a visible browser
```

---

## CLI Reference

```bash
# Check runtime health, database version, and configuration validation
python main.py --health-check

# Display SQLite row counts, integrity check, and foreign key status
python main.py --db-status

# Send an SMTP test email
python main.py --send-test-email

# List recent projects in terminal
python main.py --list-projects --limit 30

# Export complete project database to CSV
python main.py --export-csv data/freelancermap_projects.csv

# Run in dry-run mode (scans and stores projects without sending emails)
python main.py --dry-run --run-once

# Log in once in a visible browser and persist the authenticated session
python main.py --interactive-login --visible

# Create the baseline (stores existing projects without emailing them)
python main.py --initialize-baseline --visible

# Validate the browser, session, and listing load end-to-end
python main.py --test-browser --visible

# Print the resolved primary/personalized feed URLs and feature toggles
python main.py --show-search-configuration
```

---

## Browser & Chrome Options

- The monitor uses a **persistent profile** (`data/chrome_profile`, `--profile-directory` support) so login state survives across runs.
- A cross-process profile lock prevents two monitor instances from sharing the same Chrome profile; the lock is released even when Chrome fails to start.
- No automation-masking flags and no spoofed user agent are used — the monitor presents itself as a normal Chrome browser.
- `--no-sandbox` / `--disable-dev-shm-usage` are **opt-in** via `CHROME_NO_SANDBOX=true` for constrained Linux/container deployments only.

---

## Testing & Reliability Engineering

Every push and pull request is verified automatically by the [CI pipeline](https://github.com/Paramount-Intelligence/freelancermap-monitor/actions/workflows/ci.yml) on **8 jobs** (Python 3.10–3.13 × Ubuntu + Windows). Each job runs a 10-step pipeline: dependency installation, `pip check`, bytecode compilation, the complete unit-test suite, the static source-integrity audit, and the isolated synthetic end-to-end smoke test. A red pipeline blocks merging — the repository is never advertised as green on locally-only runs.

```bash
# Run complete test suite (299 tests passing)
python -m unittest discover -s tests -v

# Run property-based 1,000-input fuzz testing
python -m unittest tests/test_parser_fuzz.py

# Run adversarial edge-case test suite
python -m unittest tests/test_adversarial_parser.py

# Run the static source-integrity audit (syntax, imports, driver-construction)
python scripts/check_source_integrity.py

# Run the synthetic 3-cycle end-to-end smoke test (no real network access)
python smoke_test.py
```

- **299 Unit Tests**: 100% passing rate across database, emailer, parser, browser, and monitor modules — including dual-feed merge/precedence/provenance, feed-status bookkeeping, authentication gating, first-run baseline safety, sort enforcement, listing-stability polling, page-load timeout fail-closed behavior, and profile-lock cleanup.
- **Source-Integrity Audit**: The CI guard (`scripts/check_source_integrity.py`) parses every Python file, validates every import alias against stdlib / `requirements.txt` / local packages, and forbids direct browser-driver construction in tests (including aliased `webdriver.Chrome()` calls and `from selenium.webdriver import Chrome`). It is covered by its own unit suite (`tests/test_source_integrity.py`).
- **Fuzz Testing**: 1,000 randomized malformed HTML inputs verifying 8 critical invariants.
- **Adversarial Suite**: 22 edge-case tests protecting against prose label pollution, split DOM nodes, and hidden modals.

---

## Safety & Ethical Guidelines

- **No Bypass Mechanics**: Does not bypass CAPTCHA, MFA, rate limits, authentication, or access controls.
- **Interactive Login**: Supports manual authentication via `--interactive-login` to safely save sessions under `data/chrome_profile`.
- **Conservative Politeness**: Employs randomized delay intervals (`4s - 8s`) between detail page requests to ensure minimal server footprint.
- **Credentials**: `FREELANCERMAP_LOGIN_EMAIL` / `FREELANCERMAP_LOGIN_PASSWORD` are read from `.env` (gitignored) only; prefer interactive login to avoid storing the password in plaintext.

---

## License & Disclaimer

Distributed under the **MIT License**. See `LICENSE` for details.  
*Disclaimer*: This software is for personal monitoring use. Always adhere to Freelancermap's terms of service and acceptable use policies.
