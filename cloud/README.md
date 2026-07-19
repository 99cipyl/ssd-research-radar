# Cloud publishing and mobile subscription

The workflow in `.github/workflows/publish-radar.yml` turns the existing local
radar into a public, mobile-friendly service for the expected repository
`99cipyl/ssd-research-radar`.

## Public endpoints

After GitHub Pages is enabled with **Source: GitHub Actions**:

- Dashboard: `https://99cipyl.github.io/ssd-research-radar/`
- Recommended NetNewsWire feed: `https://99cipyl.github.io/ssd-research-radar/full.xml`
- Recent new/update events only: `https://99cipyl.github.io/ssd-research-radar/feed.xml`
- NetNewsWire live + chunked-history OPML: `https://99cipyl.github.io/ssd-research-radar/netnewswire.opml`
- Compatibility OPML alias: `https://99cipyl.github.io/ssd-research-radar/subscriptions.opml`
- Machine-readable source health: `https://99cipyl.github.io/ssd-research-radar/status.json`
- Per-item Chinese brief: `https://99cipyl.github.io/ssd-research-radar/item.html?id=<public_id>`

Subscribe to `live.xml` for daily updates, and import `netnewswire.opml` once
when the historical archive is wanted. The OPML contains the live feed plus
year/chunk archive feeds, each capped at 350 items. `full.xml` remains an
optional machine-readable export of the whole archive, but server-side readers
may truncate a single feed that large.

Each RSS item links to the local Chinese brief first; the original source is a
separate button inside that page. New/update events are withheld from every
subscribed feed until a validated professional brief exists. Each professional
brief includes an exact short supporting quote, explicit evidence level, model,
generation time, and an AI-not-human-reviewed label. The public feeds advertise
a Google WebSub hub, which the workflow pings only after Pages deployment. Server-side readers that
support WebSub can therefore learn about a published update without waiting for
their next polling cycle.

## State and failure behavior

The workflow checks the RSS/WordPress/specification sources at minutes 07, 22,
37, and 52 of every hour. FAST and OpenAlex are checked once daily because they
are slower academic indexes rather than immediate announcement feeds, and they
receive an explicit full rescan on the first day of each month. A manual run
or a relevant `main`-branch source/configuration push checks every source. A
single concurrency group prevents overlapping syncs.

The durable database is stored in the orphan `radar-state` branch. Runtime-only
changes such as `last_attempt_at`, `last_success_at`, empty `runs`, and delivery
acknowledgements do not affect the material fingerprint. Consequently, an empty
15-minute poll does not commit another large SQLite file. A commit occurs only
when an item, source mapping, version snapshot, or notification event changes.
Before every state push the database is integrity-checked, checkpointed, pruned
of empty historical runs, and vacuumed. Structured briefs and the durable
attempt counter are part of the material fingerprint, while retry timestamps
and transient model errors are not.

`radar.py sync` exit code 2 means at least one source failed but the remaining
sources completed. The workflow intentionally continues through Pages
deployment so `status.json` and the dashboard show that failure. It then marks
the workflow run failed, preserving the operational warning. If the same run
also discovered content, that content and its already-delivered event are
persisted before deployment, so the event is not silently discarded.

## Optional secrets

The workflow works without user-created secrets and uses each source's documented public
fallback. Add these repository Actions secrets when available:

- `OPENALEX_API_KEY`: improves OpenAlex quota and reliability.
- `GROUPS_IO_API_KEY`: enables the complete OCP Storage Groups.io history;
  without it, OCP falls back to the latest 20 public RSS messages and continues
  accumulating future messages.

Secrets are passed only as process environment variables. They are never added
to the site, report, state branch, or logs by the cloud helper scripts.

Professional summaries use the job's short-lived built-in `GITHUB_TOKEN` with
the workflow's `models: read` permission and the `openai/gpt-4.1-mini` GitHub
Models endpoint. Manual runs backfill 12 recent historical items; the daily
academic and monthly runs each backfill 24. Live events are attempted first,
at most 12 per run, and do not count against those history limits. Failed items
wait six hours before retry, and one run spends at most five minutes scheduling
model work.

## Timing limits

This is near-real-time RSS, not a hard real-time push system:

- GitHub Actions schedules are best-effort and can be delayed during high load.
- Scheduled workflows in public repositories are disabled after 60 days with no
  repository activity; re-enable the workflow manually if that ever happens.
- The cloud can detect and publish within roughly 15 minutes, but upstream APIs
  can expose records later than their original publication time.
- NetNewsWire on iOS notifies only after iOS lets it download the article;
  background refresh timing is controlled by iOS. For the quickest phone push,
  subscribe to `full.xml` through a WebSub-aware server account such as Feedbin
  and use Feedbin Notifier, then connect that account inside NetNewsWire.

The initial cloud run has no state branch, so it performs the full baseline and
can be much slower than later runs. That baseline is deliberately not emitted as
thousands of "new" events; it is instead present as the historical portion of
`full.xml` and in the searchable dashboard.
