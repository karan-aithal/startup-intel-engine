# Decisions (Phase 0)

| # | Topic | Decision |
|---|---|---|
| 1 | Repo location | `~/startup-intel-engine` (ext4). The NTFS folder holds only the prompt. |
| 2 | Database | Existing PostgreSQL 18 on 127.0.0.1:5432. Dedicated db + role `startup_intel`. pgvector via `postgresql-18-pgvector`. |
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
