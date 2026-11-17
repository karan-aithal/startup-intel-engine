# Decisions (Phase 0)

| # | Topic | Decision |
|---|---|---|
| 1 | Repo location | `~/startup-intel-engine` (ext4). The NTFS folder holds only the prompt. |
| 2 | Database | Existing local PostgreSQL (14 on dev box) on 127.0.0.1:5432. Dedicated db + role `startup_intel`. pgvector via `postgresql-<ver>-pgvector` (PGDG repo added by db_setup.sh; Ubuntu jammy lacks it). |
| 3 | Docker | Install now (`scripts/install_docker.sh`, user runs with sudo). Used for Prometheus/Grafana later. |
| 4 | Python | 3.12 via `uv` in `~/.local/bin`. |
| 5 | Uptime | Up to ~12 h/day, not 24/7. Collectors need catch-up after downtime (cursors, backfill). |
| 6 | Ports | API 8000, dashboard 3000, localhost only. |
| 7 | Git | Local repo now; GitHub remote supplied by user after Phase 0. |
| 8 | Stack | FastAPI, SQLAlchemy 2 async, Alembic, Next.js + TypeScript + SCSS, npm. |
| 9 | Telegram | Walk through BotFather (Phase 2). |
| 10 | Topics | All suggested topics for now; final list decided later. |
| 11 | Job categories | Software engineering, AI/ML/data, cloud/DevOps, cybersecurity, product, design. |

| 12 | Seniority | intern, fresher, junior, mid, senior, founding roles |
| 13 | India cities | Bengaluru (Karnataka), Chennai, Hyderabad. Remote India is also high priority. |
| 14 | Global scope | Remote roles only, any country. |
| 15 | Alert inbox | User has a Gmail account (dedicated alert account to be confirmed; IMAP app password). |
| 16 | Portals | Accounts on all job portals; alerts may be set up on each. |
| 17 | Telethon | Not decided. User asked for alternatives to Telegram sources (see Phase 4 questions). |
| 18 | Reddit | No approved credentials. Off. User wants a compliant substitute for community signals (Phase 4). |
| 19 | Access | localhost only. |
| 20 | Backups | `~/backups/startup-intel`, daily 20:00 IST, 7 daily + 4 weekly. |
| 21 | Raw content retention | 90 days (shorter where a source's terms require). |

## Still open
Alert Gmail address (dedicated or personal), Telegram bot token (Phase 2), final Telegram topic list, Telethon choice.

## Phase 2 decisions (2026-11-11)
| # | Topic | Decision |
|---|---|---|
| 22 | n8n | Optional extra, Phase 8 or later. Fed by an outbound webhook from the engine. The engine must work without it. |
| 23 | Telegram bot and supergroup | User creates later (BotFather walkthrough deferred). Token goes in `.env` by the user. Until then alerts go to a console/log sink. |
| 24 | Phase 2 topics | India Funding, Global Funding, India Hiring, Global Hiring, Unverified and Risk, System Health. |
| 25 | Funding feeds, ATS tokens | Lists in `docs/phase2-proposals.md` accepted as recommended (15 feeds, 30 tokens). |
| 26 | SEC User-Agent contact | Email supplied by user later. SEC connector stays disabled until then. |
| 27 | Gold set | Assistant pulls and pre-labels 50-100 real items; user corrects. |
| 28 | Dependencies | feedparser, httpx, python-telegram-bot. |

## Phase 4 decisions (2026-11-15)
| # | Topic | Decision |
|---|---|---|
| 29 | Watchlists | Telegram channels and Bluesky accounts are edited in the UI (`/watchlists`), stored in the DB. Removing disables, history kept. |
| 30 | Mastodon | Excluded. |
| 31 | X.com | No scraping or paid API. Forward X links to the Telegram bot (`share_to_bot` route, `TELEGRAM_OWNER_ID` allow-list). |

## Phase 5 decisions (2026-11-15)
| # | Topic | Decision |
|---|---|---|
| 32 | Auto-watch cap | 200 companies (`AUTO_WATCH_CAP`). Past it, found ATS boards are stored as `candidate` and not polled. |
| 33 | Common Crawl ATS seeding | Yes, monthly, CDX index queries only (no WARC downloads). Boards stored as candidates. Route "Common Crawl ATS" is enabled by `seed_sources.py --enable`. |
| 34 | Hot companies | Funded companies polled every 10 min for 90 days, then 30 min. |

## Phase 6 decisions (2026-11-15)
| # | Topic | Decision |
|---|---|---|
| 35 | Model runtime | llama.cpp `llama-server` (binary from LM Studio's backend, `scripts/llm_servers.sh`), OpenAI-compatible API (`LLM_BASE_URL`, `LLM_EMBED_BASE_URL`). LM Studio's own server is not used: it cannot turn Qwen3.5 thinking off. Ollama is not used. Models live in `/media/xlr8/6A025A89025A59E1/Users/karan/.lmstudio/models`. |
| 36 | Local model | `Qwen3.5-4B` Q4_K_M (lmstudio-community GGUF), CPU-only. Set `LLM_ENABLED=true` once the server is up. |
| 37 | Embeddings | `nomic-embed-text-v1.5` GGUF (768 dims), pgvector HNSW. Funding news with cosine similarity >= 0.92 within 14 days is marked `duplicate_of`. |
| 38 | Isolation | LLM work is its own queue kind (`llm_enrich`), one job at a time on its own worker loop. Alerts never wait for it. |

Architecture revision plan (Phase 6 follow-ups, before Phase 7): see `docs/architecture-plan.md`.

## Architecture revision decisions (2026-11-16)
| # | Topic | Decision |
|---|---|---|
| 39 | Board cap | 300 active auto-registered ATS boards, enforced separately from the 200-company cap (`AUTO_BOARD_CAP`). Candidates are promoted when room exists: hot, then India-linked, then oldest. |
| 40 | Duplicate alerts | Confirmed duplicates (same company and amount, or identical content from another source, same label) are linked in `event_relationships` and not re-alerted. A rumour then an announcement is a follow-up and both alert. Semantic matches only record a link. |
| 41 | Grounding gate | 0.95 label accuracy on the gold set. Passed at 0.96. |
| 42 | Claim staging | Regex and LLM extractions merge into `funding_round_claims`, `company_investor_claims`, `company_founder_claims` (status `unverified`). Authoritative tables are written only after Phase 7 verification. |
| 43 | Outbox | At-least-once. A re-sent message after a crash is flagged "(resent)". |
| 44 | Migrations | A pushed migration is never edited; fixes ship as a new idempotent revision (0005 renames the doubled check-constraint names from 0004). Migrations are verified with `scripts/migration_check.py` in a scratch schema, not by pytest, which builds its schema from the ORM. The script fails on drift, on constraint/index name mismatches, on changed real rows, and if `public` changes. |
| 45 | Duplicate matching order | Verified on live data: jobs run in queue order, not detection order, so the sync rule must not depend on which article was detected first. Matching now considers any already-classified event within +-7 days; an event that already alerted never becomes a duplicate on retry; a headline whose company the extractor cannot isolate matches when the other article's company is named in it and amount and currency are equal. Known limit: two articles that never name the same company in the headline (e.g. "Freshworks alums raise $53M" vs "Dreamteam raises $53M") are only linked after LLM enrichment, so the second alert is sent; Phase 7 correlates them through the claims table. 3 such duplicate alerts (of 129) predate the fix and cannot be recalled. |
| 46 | Claim routing | The model label routes claims per source event: `funding` and `rumour` stay `unverified`; `none` sets the event's funding, investor and founder claims to `rejected` and stamps `evidence.rejected_by = llm_label_none` on the funding claim. Reversible: a later run that changes the label rewrites only rows carrying that marker; a rejection made by Phase 7 (no marker) is never overwritten. Rejected claims stay for audit; Phase 7 corroborates only `unverified` claims. Three duplicate alerts (Bharat Semi, Stark Future, Dreamteam) were delivered before the dedup fixes and are kept as historical incidents; they cannot be recalled. |

## Phase 7 decisions (2026-11-16, proposed defaults in `config/scoring.yaml`; not yet confirmed by the owner)
| # | Topic | Decision |
|---|---|---|
| 47 | Authoritative writes | A claim cluster becomes a `funding_rounds` row only at PARTIALLY_VERIFIED or better. Unverified claims (community-only, rumour-only) stay claims, with no round. Investors and founders are promoted only from a VERIFIED round, with the evidence row as proof. |
| 48 | Evidence type per source | Mapped by source name in `scoring.yaml`; wires and SEC filings count as official, named publications as reputable (90), the inbox as discussion (55), anything unlisted (Hacker News, Bluesky, share-to-bot) as community (25). Assumption: every PR Newswire VC item is a company-issued release. |
| 49 | Independence | Events that are `syndicated_copy` of each other, share a content hash or come from the same source are one syndication group and count once. Separate articles from different outlets are independent. Known limit: two outlets rewriting one press release look independent. |
| 50 | Status rules | VERIFIED: an official-class source, or two independent sources at weight >= 90. PARTIALLY_VERIFIED: one source >= 85, or two independent >= 55. Rumour-only is never above UNVERIFIED and its confidence is capped at 40. EXPIRED: PARTIALLY_VERIFIED with nothing newer for 60 days. Confidence = best weight + 5 per extra independent source (max +10). |
| 51 | Contradiction | Same company, same currency, same 14-day window, different amounts, both at least partially verified: the round that is 10+ confidence points weaker becomes CONTRADICTED and the winner stores its articles as `supports=false` evidence; with no clear winner neither can be VERIFIED. Both lose 10 confidence and carry `conflicting_amount`. Different currencies are never compared (no FX source). |
| 52 | Scores | F and E from PROMPT.md, stored as `score_india` / `score_global` on funding rounds and `emerging_india` / `emerging_global` on companies. Global drops the India term and renormalises. Risk flags subtract fixed penalties. Hiring score H is NOT built: nothing writes `job_postings` yet (jobs exist only as source events). |
| 53 | Correlation | Rule-based signals in `company_signals` (idempotent by `signal_key`): `funding_then_hiring`, `hiring_surge`, `senior_hiring`, `new_investor`. Jobs are tied to companies through `ats_boards` and dated by the job's own `published_at`. Not built: new country (job `country` is always null), new office and product launch (no data source). Every signal says it is a sequence, not a cause. |
| 54 | Alert thresholds | Not wired. Instant / hourly digest / silent cut-offs need the owner's confirmation first (PROMPT.md "Scores"). |
| 55 | Migration 0006 | Adds claim-to-round link, score columns, `company_signals.signal_key` unique, evidence (round, event) unique. `migration_check.py copy` now builds the scratch schema at the live revision instead of 0002 (0003+ tables could not be copied before). |
| 56 | Wire and SEC gating | `press_wire` is no longer an official type: a wire publishes whatever an issuer sends, so PR Newswire VC alone is PARTIALLY_VERIFIED (90) and needs one independent source to reach VERIFIED. An SEC Form D counts as `official_filing` only when its issuer (named in the filing title) canonicalizes to the claim's company; otherwise it is scored as community and the round carries `issuer_mismatch`. Form D events carry no amount or stage today, so they cannot form a round on their own yet. |
| 57 | Stage relevance | Proposed: series a 100, seed 90, pre-series a 85, series b 80, pre-seed 70, series c 55, bridge/strategic 40, unknown 50 (stages where startups ramp hiring score highest). Risk penalties remain provisional. |

## Phase 8 decisions (2026-11-17, proposed defaults in `config/acquisition.yaml`)
Owner scope: core infrastructure and GitHub go; Jooble India pilot conditional (disabled until the API key is registered and permitted use is confirmed); other aggregators, YouTube and Product Hunt on hold; Discord deferred. Existing funding, hiring, verification and alert paths, alert semantics and scoring thresholds are unchanged.
| # | Topic | Decision |
|---|---|---|
| 58 | Adaptive polling | Next poll = base interval x activity factor (score >= 70: x0.5, >= 30: x1, >= 10: x1.5, else x2, nothing new in 7 days: x4), clamped to 60 s .. max(3,600 s, base), +-10% jitter. Hot-company ATS boards never slow down. Activity score = min(100, 25 x log2(1 + new items per day)) over 7 days, refreshed hourly. Due routes run in order of priority, then activity. Routes with no `next_poll_at` (all existing ones) keep the old "last run + interval" rule until their first run. |
| 59 | Quota manager | Persisted token bucket plus a hard window cap per provider (`rate_limits`), shared by all routes of a provider and surviving restarts. Caps sit below published limits: GitHub 4,500/h with a token (limit 5,000), 50/h without (60); Jooble 90/day (free plan 100, resets 00:00 UTC). A 429/403 that names a limit blocks the provider until `retry-after` / `x-ratelimit-reset`. A rate limit defers the route; it never counts as a failure. |
| 60 | Circuit breaker | 3 consecutive failures open the circuit for 300 s, doubling per further failure up to 6 h, then one trial; a success closes it. Not tied to `enabled`, so a tripped source recovers by itself. |
| 61 | Source fallback | A route with `config.fallback_for = <primary>` is polled only while the primary is enabled and its circuit is open or it is UNAVAILABLE. Mechanism only: no live pair is configured (Hacker News RSS next to Hacker News Algolia is the obvious candidate; it would change today's collection, so it needs a separate decision). |
| 62 | Watchlist expansion | Discovered company feeds and careers pages (registered disabled by Phase 4/5, never enabled until now) are switched on up to `AUTO_SOURCE_CAP`, default 0 = off until the owner picks a number. Eligible: watched, hot or funded (partially verified or better in 180 days). Ranking: hot, verified funding, India-linked, emerging score, oldest. Demoted on 3 failures, a muted company, or 30 days idle. GitHub feeds are excluded: the GitHub route covers them. |
| 63 | GitHub | `GitHub Orgs` route (disabled by `--enable` unless `GITHUB_TOKEN` is set): one `GET /orgs/{org}/repos?sort=created` per organisation per run, ETag conditional requests, forks and archived repos skipped, first poll reports only repos from the last 30 days. Events are `repo` type: stored and turned into a `new_repo` company signal, never classified for alerts. Organisations come from discovered GitHub profiles of watched, hot or funded companies (cap 100). Verified against the live API (one anonymous request). Docs: docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api, .../best-practices-for-using-the-rest-api, .../rest/repos/repos#list-organization-repositories. |
| 64 | Jooble | `POST {JOOBLE_BASE_URL}/{key}` with `{keywords, location, page}`, 5 keywords every 4 h (30 of 90 daily requests). Off by default and gated twice: `jooble.terms_confirmed: true` in `config/sources.yaml` plus `JOOBLE_API_KEY`; `--enable` never switches it on. Docs: jooblehelpcenter.freshdesk.com/en/support/solutions/articles/60001448238. Open: Jooble publishes no API-specific terms and its general terms ban automated access to the site, so permitted use needs the owner's own confirmation from Jooble; each country has its own key and domain, so confirm `JOOBLE_BASE_URL` for India. Jobs enter the normal hiring path unchanged (48 h alert age limit applies). Errors never contain the URL (the key is in the path). Only tested against mocks: no key exists yet. |
| 65 | Migration 0008 | `source_registry.next_poll_at / activity_score / circuit_open_until`, table `rate_limits`. |
| 66 | GitHub attribution | An organisation's new repos are credited to a company only when the org login and the company name contain one another (letters and digits, 4+ characters). Phase 4 accepts any GitHub link on a company's site, including partners and vendors (Mosaico's site links `espressif`), so without this gate a partner's repos would inflate the company's growth signal. Dot-repos (`.github`, the organisation profile repository) are skipped at ingest. |
| 67 | Ignored events | Repo events can be `processing_status = 'ignored'`: kept for audit, excluded from signals, and stable across retried jobs (`process_event` re-derives `ignored` from the repo name or `raw_metadata.ignored_reason`). Used once to retire the Mosaico `new_repo` signal that came from `esp-mosaico/.github`. |
| 68 | Backup tooling | `backup.sh` and `restore.sh` use the client matching the server's major version (the PATH `pg_dump` was v14 against server v18.7, so every backup failed). Restore tested 2026-11-17 into a scratch schema built from the migrations: all 32 tables identical to live (18,527 rows) and all 51 foreign keys validated. A restore into an empty database is not tested: the role has no CREATEDB. |
