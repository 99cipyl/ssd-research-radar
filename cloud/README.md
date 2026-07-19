# Cloud publishing and mobile subscription

The workflow in `.github/workflows/publish-radar.yml` turns the existing local
radar into a public, mobile-friendly service for the expected repository
`99cipyl/ssd-research-radar`.

## Public endpoints

After GitHub Pages is enabled with **Source: GitHub Actions**:

- Dashboard: `https://99cipyl.github.io/ssd-research-radar/`
- Recommended NetNewsWire feed: `https://99cipyl.github.io/ssd-research-radar/full.xml`（只含通过证据校验的专业简报）
- Recent new/update events only: `https://99cipyl.github.io/ssd-research-radar/feed.xml`
- NetNewsWire live + chunked-history OPML: `https://99cipyl.github.io/ssd-research-radar/netnewswire.opml`
- Compatibility OPML alias: `https://99cipyl.github.io/ssd-research-radar/subscriptions.opml`
- Machine-readable source health: `https://99cipyl.github.io/ssd-research-radar/status.json`
- Per-item Chinese brief: `https://99cipyl.github.io/ssd-research-radar/item.html?id=<public_id>`

Subscribe to `live.xml` for daily updates, and import `netnewswire.opml` once
when the historical archive is wanted. The OPML contains the live feed plus
32 immutable hash-bucket archive feeds. Their URLs are pre-created and a
canonical item never changes bucket; only professionally validated rows are
emitted. This lets one import receive later discoveries and backfill without
another OPML import. `full.xml` is the single-feed professional export, but
server-side readers may truncate it as it grows.

Each RSS item links to the local Chinese brief first; the original source is a
separate button inside that page. Historical, new, and updated items are all
withheld from every subscribed feed until a validated professional brief exists. Each professional
brief includes an exact short supporting quote, explicit evidence level, model,
generation time, and an AI-not-human-reviewed label. The public feeds advertise
a Google WebSub hub, which the workflow pings only after Pages deployment. Server-side readers that
support WebSub can therefore learn about a published update without waiting for
their next polling cycle.

## State and failure behavior

The workflow checks the RSS/WordPress/specification sources at minutes 07, 22,
37, and 52 of every hour. FAST and OpenAlex are checked once daily because they
are slower academic indexes rather than immediate announcement feeds. On the
first day of each month, the explicit `--full` job enables only FAST and replays
its bounded TOC set. OpenAlex performs an initial historical baseline and then
uses a rolling one-year daily search; its 25-year keyword history is not replayed
monthly because a late free-quota 429 would discard the whole fetched batch. A
manual run or a relevant `main`-branch source/configuration push checks every
source while likewise suppressing an automatic OpenAlex full replay. A single
concurrency group prevents overlapping syncs.

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

The non-OpenAlex sources retain documented public fallbacks without
user-created secrets. Add these repository Actions secrets when available:

- `OPENALEX_API_KEY`: strongly recommended for OpenAlex quota and reliability;
  without it, an OpenAlex poll may be rate-limited while the other sources keep
  running.
- `GROUPS_IO_API_KEY`: enables the complete OCP Storage Groups.io history;
  without it, OCP falls back to the latest 20 public RSS messages and continues
  accumulating future messages.

Secrets are passed only as process environment variables. They are never added
to the site, report, state branch, or logs by the cloud helper scripts.

Professional summaries use the job's short-lived built-in `GITHUB_TOKEN` with
the workflow's `models: read` permission and the `openai/gpt-4.1-mini` GitHub
Models endpoint. Manual runs backfill 12 recent historical items; the daily
academic run backfills up to 120 within a 20-minute model budget. Frequent and
monthly runs set the historical backfill limit to zero, so the monthly FAST
rescan does not duplicate the daily academic model batch. Live events are
attempted first, at most 12 per run, and do not count against those history
limits. Failed items wait six hours before retry. Frequent, manual, and monthly
runs spend at most five minutes scheduling model work; only the daily academic
historical backfill may spend up to twenty minutes.

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
thousands of "new" events. The complete catalogue remains searchable on the
dashboard; each historical item enters `full.xml` and its fixed archive feed
only after the professional brief passes validation.
