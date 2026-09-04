# flightwatch

Watches award availability across frequent-flyer programs and alerts you when a
redemption drops below a mileage threshold you set.

Data comes from the [seats.aero](https://seats.aero) Partner API. A `mock` provider
ships alongside it so you can develop and test the whole pipeline without spending
quota.

## Setup

```sh
uv sync
export SEATS_AERO_API_KEY=...        # your Partner API key
uv run flightwatch init              # writes ~/.config/flightwatch/config.toml
$EDITOR ~/.config/flightwatch/config.toml
uv run flightwatch check             # validates config + credentials, no API calls
```

> **Your key must be a Partner API key.** A seats.aero Pro *website* subscription is
> a separate product and will not authenticate against the API. If `check` passes but
> searches return 401, that's the likely cause.

### Verify the response schema once

The parser keys off field names in the API response. Confirm them against your plan
before trusting results:

```sh
uv run flightwatch probe SFO LHR
```

This prints one raw record plus what the parser extracted from it. Two things to check:

- **Mileage costs** appear under `JMileageCost`, `YMileageCost`, etc. (`Y` economy,
  `W` premium, `J` business, `F` first).
- **Taxes.** If the surcharge figures look ~100x too large, the API is reporting minor
  units; set `taxes_in_cents = true` under `[provider]`. Otherwise `max_taxes`
  thresholds will silently never match.

## Verified against the live API

Confirmed on a real SFO→DUB business search:

- **Mileage costs are strings** in the plain fields (`"57500"`) and ints in the direct
  fields (`57500`). Both are coerced.
- **Taxes are in minor units.** United reported `YTotalTaxes: 560` with
  `TaxesCurrency: "USD"` — that's $5.60, the US September 11 Security Fee. Set
  `taxes_in_cents = true`. Leaving it false makes every `max_taxes` threshold
  effectively unreachable.
- **`Route.Distance` is statute miles**, not km (SFO–DUB reported 5090).
- **Ignore the `*Raw` fields.** Every cabin has both a plain and a `Raw` variant, and
  they disagree: one record carried `JAvailable: false` next to
  `JMileageCostRaw: 200000`. The plain fields are filtered to sane saver pricing; the
  Raw set includes dynamic-pricing noise. This parser reads only the plain fields.
- **Seat counts are often absent** for some programs (AA returned none), so `min_seats`
  won't filter those. That's the documented permissive-on-missing-data behavior.
- **`OptionalPricing`** carries co-branded card discounts (e.g. `UAELITECARD` at 34,000
  where the public price is 40,000). Not currently used.

Surcharges vary enormously at identical mileage prices — the same 57,500-mile AA award
is $5.60 routed AA/Aer Lingus and $749 routed via London on BA. Results sort by miles,
then by surcharge, so the cheap-cash options surface first; set `max_taxes` to exclude
the expensive routings outright.

## Configuration

```toml
state_path = "~/.local/state/flightwatch/state.db"

[provider]
kind = "seats_aero"            # or "mock"
api_key_env = "SEATS_AERO_API_KEY"
requests_per_minute = 60       # client-side throttle; keep at or below your plan's limit
taxes_in_cents = true          # correct for seats.aero -- see the section above

[dedupe]
cooldown_days = 3              # re-alert an unchanged award after this long (0 = never)
realert_on_improvement = 5000  # ...or immediately if it drops this many miles

[[notify]]
kind = "console"

[[watch]]
name = "West coast -> Europe, business"
origins = ["SFO", "OAK", "SJC"]
destinations = ["LHR", "CDG", "AMS"]
programs = ["aeroplan", "flyingblue", "virginatlantic"]   # omit for all programs
depart_after = "+21d"          # ISO date, or a relative offset: +21d, 6m, 1y, today
depart_before = "+300d"
airlines_exclude = []          # e.g. ["AC"] to skip a carrier

  [[watch.thresholds]]
  cabin = "business"           # economy | premium | business | first
  max_miles = 70000            # inclusive: 70,000 qualifies
  # under_miles = 70000        # ...or strict: 70,000 does NOT qualify. Set one, not both.
  max_taxes = 150              # optional cash-surcharge cap
  min_seats = 2                # optional; awards showing fewer seats are skipped
  nonstop_only = false
```

Each `[[watch]]` can carry several thresholds — one per cabin. Add as many watches as
you like; they're searched in sequence and a failure in one doesn't abort the others.

### How thresholds are applied

Each threshold takes exactly one ceiling: `max_miles` is **inclusive** (an award at
exactly that price qualifies) or `under_miles` is **strict** (it doesn't). Award prices
cluster on round numbers, so an award sitting precisely on your limit is a real case,
and setting neither or both is a config error rather than a silent default. Missing data is
treated permissively on purpose: if the API reports no seat count or no tax figure,
the award still passes `min_seats` / `max_taxes` rather than being hidden from you.
The alert shows `-` so you know to check.

Providers report both a cheapest-any-routing price and a cheapest-nonstop price for
the same route-day. Both are evaluated, then collapsed to a single alert — the
cheapest one, preferring nonstop on a tie — so one piece of award space never
produces two notifications.

### Pausing a watch

```toml
[[watch]]
name = "..."
enabled = false
```

A paused watch is skipped before any search runs, so it costs no API calls. Its alert
history is kept, so re-enabling it does not replay deals you were already told about.
`flightwatch check` shows paused watches and warns if every watch is off.

Notifiers scoped to a paused watch stay valid, so you can pause and resume without
touching the `notify` blocks.

### Return trips

Add `return_trip` to pair each qualifying outbound with a return leg:

```toml
return_trip = { min_days = 7, max_days = 14 }
```

The reversed route is searched over a window wide enough to cover any outbound in
range, and returns are held to the **same thresholds** as the outbound — same mileage
ceiling, same tax cap, same programs, same cabin.

Behaviour worth knowing:

- **One-way alerts are unaffected.** A deal with no qualifying return is still alerted
  on its own; the return is extra information when it exists, never a filter.
- Returns are collapsed per route-day-cabin and the **cheapest is shown**, capped at 3
  with the remaining count noted (`4 other date(s)`).
- The **return search only runs when something qualified outbound**, so a watch that
  finds nothing costs no extra API call.
- A failed return search **does not lose the outbound deals** — it's reported as an
  error and the one-way alerts still go out.
- Dedupe is round-trip aware: a return **appearing** where there was none re-alerts,
  and so does a round-trip total that drops by `realert_on_improvement`, even when the
  outbound price hasn't moved. A return disappearing does not.

State files created before this feature are migrated in place (a `return_miles` column
is added), so existing alert history survives rather than triggering a fresh wave.

### Restricting to credit-card transfer partners

Use an alias instead of listing programs by hand:

```toml
programs = ["@chase"]        # Chase Ultimate Rewards airline transfer partners
programs = ["@chase", "alaska"]   # aliases and explicit programs combine
```

`@chase` resolves to `aeroplan`, `british`, `flyingblue`, `jetblue`, `united`,
`virginatlantic`. Two caveats worth knowing before you rely on it:

- **American AAdvantage and Alaska Mileage Plan are not Chase partners**, and on some
  routes they hold all the cheap space. On SFO–DUB they had every award under 80k while
  the only Chase option was United at 120,000.
- **Chase partners seats.aero doesn't index:** Aer Lingus AerClub, Emirates Skywards,
  Iberia Plus, Singapore KrisFlyer, Southwest. Aer Lingus is the notable gap for
  Ireland, since it operates the nonstops — a Chase-restricted watch cannot see them.

Unknown program names are rejected at config load. A typo that silently matched nothing
would be indistinguishable from "no award space", which is the worst failure mode here.
Valid programs are listed in `programs.py`.

### Overlapping watches

Watches may overlap deliberately — say a `@chase`-restricted watch alongside a broad
one. An award matching both is alerted once per run, attributed to whichever watch you
listed **first** in the config, so put your most specific watch at the top.

### Scoping a channel to specific watches

By default every channel gets every deal. Add `watches` to restrict one:

```toml
[[notify]]
kind = "resend"
sender = "onboarding@resend.dev"
recipients = ["you@example.com"]
watches = ["SFO -> Dublin, business, Chase-transferable"]   # email only for this watch

[[notify]]
kind = "console"    # unscoped: still sees everything
```

Watch names are validated at load time, so renaming a watch fails loudly instead of
silently disabling the channel.

**Keep at least one unscoped channel.** Delivery is tracked per deal: a deal that
reaches no channel is *not* recorded as seen, and the run reports how many were
dropped and from which watches. That prevents silent loss, but it also means a deal
with no channel covering it will resurface every single run until one exists.

### When a channel fails

A deal is recorded as seen once a channel that **counts as delivery** accepts it. If
every counting channel fails, the deal is not recorded and resurfaces next run, so a
transient outage costs you a delay rather than the alert.

```toml
[[notify]]
kind = "console"
counts_as_delivery = false   # this channel is an echo, not a delivery guarantee
```

Set `counts_as_delivery = false` for any channel nobody actually reads — notably
`console` under cron or CI, where output goes to a log file. This is not theoretical: on
2026-09-04 a Telegram send timed out, the console channel wrote to the Actions log and
reported success, and the deal was recorded as delivered. It stayed suppressed for the
full 3-day cooldown and the notification was simply lost.

Telegram sends also retry on read timeouts, network faults, `5xx` and `429` (honouring
the server's `retry_after`). A `4xx` such as *chat not found* is a configuration problem
retrying cannot fix, so it fails immediately with the API's own explanation.

### Booking links

`provider.booking_url_template` controls the link in alerts. **It is currently set to
`""`, which disables links entirely** — every notifier omits the link rather than
printing one that doesn't work.

To turn links back on, put a URL back in the template:

```toml
booking_url_template = "https://seats.aero/search?min_seats=1&applicable_cabin=any&additional_days=true&additional_days_num=7&max_fees=999999&disable_live_filtering=true&date={date}&origins={origin}&destinations={destination}&view=default"
```

Supported placeholders: `{origin}`, `{destination}`, `{date}` (ISO), `{source}`,
`{record_id}`.

Why they're off, and what to know before re-enabling:

- The `/search` URL above **populates the origin and destination but never runs the
  search**, so you land on an empty results page. Every parameter from a real UI URL is
  present and in the original order; trimming any of them makes it worse, not better.
- **The param names are plural** — `origins`/`destinations`. Singular names are ignored.
- **`max_fees` filters the site's own results**, in cents. At the UI default of `40000`
  ($400) the link hides the very award being alerted whenever surcharges exceed that,
  which is routine on Virgin Atlantic and British Airways.
- **The iOS app only claims `/ioslogin/*` and `/i/*`** in its
  `apple-app-site-association`, so a `/search` link can never open the app — it will
  always land in the browser. A `/i/{record_id}` link might, but the format is
  unconfirmed and the site blocks automated probing.

## Alerts

Configure one or more channels. Every channel gets every batch.

```toml
[[notify]]
kind = "telegram"
bot_token_env = "TELEGRAM_BOT_TOKEN"
chat_id_env = "TELEGRAM_CHAT_ID"

[[notify]]
kind = "resend"
sender = "onboarding@resend.dev"     # or an address on a domain you verified in Resend
recipients = ["you@example.com"]
api_key_env = "RESEND_API_KEY"
# reply_to = "you@example.com"

[[notify]]
kind = "slack"
webhook_url_env = "SLACK_WEBHOOK_URL"

[[notify]]
kind = "email"                         # plain SMTP, as an alternative to Resend
host = "smtp.gmail.com"
port = 587
sender = "you@gmail.com"
recipients = ["you@gmail.com"]
username_env = "SMTP_USERNAME"
password_env = "SMTP_PASSWORD"         # Gmail: an app password, not your login
```

**Telegram is the best default for a frequent schedule:** free, unmetered, and pushes
to your phone. Setup:

```sh
# 1. In Telegram, message @BotFather and send /newbot
# 2. Put the token in .env as TELEGRAM_BOT_TOKEN=...
# 3. Send your new bot any message -- it cannot find you until you do
uv run flightwatch telegram-setup     # validates the token, prints your chat id
# 4. Put that chat id in .env as TELEGRAM_CHAT_ID=...
uv run flightwatch test-notify
```

`telegram-setup` reads the token from the environment rather than a CLI argument, so it
never lands in your shell history.

Telegram embeds the bot token in the request URL, and httpx logs request URLs, so a
`-v` run or a launchd job with `StandardOutPath` would otherwise write the live token
to disk. All log output goes through a redacting formatter (`redact.py`) that masks
bot tokens, API keys, `Authorization` headers and webhook paths. If you add a channel
with a differently-shaped secret, add a pattern there too.

**Watch the quotas on email.** Resend's free tier is metered (the response headers
report your remaining daily/monthly allowance), and a 30-minute schedule can exhaust it.
Telegram and Slack have no comparable limit.

`onboarding@resend.dev` is Resend's shared sender: it needs no domain setup but only
delivers to the address on your own Resend account. For anything else, verify a domain
in Resend and use an address on it — otherwise Resend rejects the send, and flightwatch
surfaces its explanation rather than a bare status code.

Resend emails go out as an HTML table with a per-deal booking link, plus a plain-text
alternative.

Test them without waiting for real award space:

```sh
uv run flightwatch test-notify
```

If **every** channel fails, the deal is *not* recorded as seen, so it resurfaces on the
next run rather than being lost. If at least one channel succeeds, it's recorded.

## Running on a schedule

A GitHub Actions workflow (`.github/workflows/watch.yml`) runs `flightwatch watch`
every 30 minutes. Credentials come from repository secrets:

| Secret | |
|---|---|
| `SEATS_AERO_API_KEY` | seats.aero Partner API key |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_CHAT_ID` | from `flightwatch telegram-setup` |

Trigger a run by hand, optionally without sending anything:

```sh
gh workflow run watch.yml -f dry_run=true
gh run list --workflow watch.yml --limit 5
```

Things worth knowing about this setup:

- **Dedupe state lives in the Actions cache.** Without it every run re-alerts
  everything. The cache key rotates per run because entries are immutable, and
  `restore-keys` picks up the most recent previous one. If the cache is ever evicted
  you get one duplicate wave, then it settles.
- **The state save runs even when the watch step fails**, because whatever was
  delivered was already recorded and losing that would re-alert it next run.
- **Runs are serialised** via a `concurrency` group. Overlapping runs would race on the
  state file.
- **GitHub's scheduler is best-effort** and commonly fires 10-30 minutes late under
  load. Treat the cadence as "roughly half-hourly".
- **Actions minutes are billed per account, not per repo.** Public repos consume none,
  which is why this one is public: on a private repo a 30-minute cadence would eat most
  of the 2,000-minute free tier shared across all your private repos, and a Free plan's
  $0 default spending limit means overruns *stop* workflows rather than bill you.
- **Cron is disabled after 60 days of repository inactivity.** GitHub emails you; one
  click re-enables it.

To run it somewhere else instead, `flightwatch watch --quiet` is the entrypoint. It
exits non-zero on error, needs the three environment variables above, and needs
`state_path` pointing somewhere that persists between runs.

## Commands

| Command | What it does |
|---|---|
| `init` | Write a starter config. |
| `check` | Validate config, credentials, and notifiers. No API calls. |
| `watch` | One full pass: search, threshold, dedupe, alert. The cron entrypoint. |
| `watch --dry-run` | Find and print deals without sending or recording them. |
| `search SFO,OAK LHR,CDG` | Ad-hoc search. Touches no state, sends no alerts. |
| `probe SFO LHR` | Dump a raw API response to verify the schema. |
| `test-notify` | Push a fabricated deal through every channel, ignoring watch scoping. |
| `telegram-setup` | Validate a bot token and discover your chat id. |
| `history` | Recent runs, with counts and errors. |
| `prune` | Forget alert history older than N days. |

## Operational notes

- **Availability is cached.** seats.aero crawls programs continuously, but a hit can
  be gone by the time you click through. Treat an alert as "go check now," not
  "confirmed bookable."
- **Broad watches make big responses.** One watch issues a single search per pass with
  all origins and destinations comma-joined across the whole date window. Pagination
  stops at 20,000 records; if you hit that ceiling you'll see a warning on stderr —
  split the watch or narrow the window.
- **State lives in SQLite** at `state_path`. Delete it to forget everything and get a
  fresh wave of alerts; run `prune` to drop only stale entries.

## Development

```sh
uv run pytest
uv run ruff check .
uv run ruff format .
```

Set `kind = "mock"` under `[provider]` to run the whole pipeline against deterministic
sample data — same query in, same results out, so it's usable in tests.

Adding a provider means implementing `search(SearchQuery) -> list[Availability]` and
`close()` (see `providers/base.py`), then wiring it into `build_provider`.
