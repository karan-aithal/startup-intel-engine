# Phase 2 proposals (for approval)

Checked 2026-11-11 from this machine with a polite User-Agent. "Responds" means the URL returned a valid RSS/Atom/JSON body. It does not mean the site's terms of use were reviewed; please skim each site's terms yourself or tell me to flag the ones you want dropped.

## Funding feeds (15 proposed)

| # | Source | URL | Region | Notes |
|---|---|---|---|---|
| 1 | Entrackr | https://entrackr.com/rss | India | `/feed` is 404; `/rss` responds. robots.txt allows. |
| 2 | Inc42 | https://inc42.com/feed/ | India | robots.txt has `ai-train=no`; we only read headlines/summaries for alerts, no model training. |
| 3 | YourStory | https://yourstory.com/feed | India | Same `ai-train=no` note. |
| 4 | Mint, Companies | https://www.livemint.com/rss/companies | India | Broad business feed, noisy. |
| 5 | The Arc | https://thearcweb.com/feed | India | Low volume. |
| 6 | Economic Times, Startups | https://economictimes.indiatimes.com/small-biz/startups/rssfeeds/11993039.cms | India | robots.txt allows. |
| 7 | TechCrunch, Startups | https://techcrunch.com/category/startups/feed/ | Global | |
| 8 | Crunchbase News | https://news.crunchbase.com/feed/ | Global | |
| 9 | EU-Startups | https://www.eu-startups.com/feed/ | Europe | |
| 10 | Sifted | https://sifted.eu/feed | Europe | Small feed (teasers). |
| 11 | TechCabal | https://techcabal.com/feed/ | Africa | |
| 12 | Rest of World | https://restofworld.org/feed/ | Global | Low funding density. |
| 13 | PR Newswire, Venture Capital | https://www.prnewswire.com/rss/financial-services-latest-news/venture-capital-list.rss | Wire | Company-authored. |
| 14 | Hacker News front page | https://news.ycombinator.com/rss | Community | Complements the Algolia API below. |
| 15 | TechCrunch, all | https://techcrunch.com/feed/ | Global | Optional; overlaps #7. |

APIs (not feeds):
- Hacker News Algolia: `https://hn.algolia.com/api/v1/search_by_date?query=<q>&tags=story` responds.
- SEC EDGAR Form D, Atom: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&count=40&output=atom` responds. SEC requires a User-Agent with contact details and limits requests to 10 per second. I'll use far less. Needs your contact email (pending).

### Not included, and why (I did not work around any of these)

| Source | Result |
|---|---|
| Tech in Asia, Business Wire, GlobeNewswire, Accesswire, Business Standard, Moneycontrol | HTTP 403 to automated requests. Treated as restricted. |
| e27 | robots.txt disallows everything. |
| VCCircle | Feed URL guess returned 404; no verified feed found. |
| Axios Pro Rata, The Ken, Term Sheet, StrictlyVC | Newsletters. Handled by the inbox in Phase 3. |

For the wire gap (Business Wire, GlobeNewswire), Google Alerts RSS or email in Phase 3 is the compliant substitute.

## ATS board tokens (30 proposed)

Every token below returned a valid job list. Honest caveat: most are well-known, late-stage companies, because those are the tokens I could verify. The emerging-startup boards come from the Phase 5 discovery loop, not from a hand-picked list. Replace any of these with companies you care about.

**Greenhouse (16):** stripe, airbnb, coinbase, figma, discord, databricks, cloudflare, datadog, gitlab, anthropic, scaleai, brex, ramp, mercury, groww, razorpaysoftwareprivatelimited

**Lever (5):** palantir, spotify, paytm, cred, zeta

**Ashby (9):** ashby, notion, linear, openai, deel, vercel, supabase, replit, sarvam

Tried and returned 404, so not used: postman, phonepe, meesho, chargebee, freshworks, clevertap (Greenhouse); netflix, swiggy, dream11, kuvera, plaid (Lever); retool (Ashby).

Endpoints used:
- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/<token>/jobs`
- Lever: `https://api.lever.co/v0/postings/<token>?mode=json`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/<token>`

Official docs to confirm (I know these links but did not re-fetch them this session, so please confirm): Greenhouse Job Board API at developers.greenhouse.io/job-board.html, Lever postings API at github.com/lever/postings-api, Ashby at developers.ashbyhq.com.

## Cost check (all genuinely free, no key or card)

All of the above are public endpoints with no key. Quotas: SEC 10 requests per second (we'll use under 1). Others publish none that I verified; I'll poll at 5–60 minute intervals with ETag and backoff.

## Gold set

I won't write recent funding rounds from memory, because I could get them wrong and a wrong gold set poisons every precision number. Instead, once you approve the feeds, I'll pull real items from them, pre-label each one (funding, hiring, rumour, noise) and give you a file of 50–100 to correct. You review, I benchmark.
