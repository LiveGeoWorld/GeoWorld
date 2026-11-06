# 2026 Safety Calendar Inputs

## Separate festival dates (2026-10-06)

`festivals.json` is a separate frozen data collection. CalendarApp now exposes
get_weekday, get_festivals and get_holidays as independent read-only queries.
Festival queries use included rows only; pending rows are not public. Historical
dataset status metadata is retained, not rewritten by the runtime integration.
It does not replace `calendar.json` or change generation date allocation.
The six `sources/timeanddate_*_20261006.json` files preserve extracted factual
table rows, URL, source line and retrieval date. Retrieval used the web tool's
rendered page because direct HTTP returned 403; these are not original HTML
snapshots and contain no claimed original-response body hash.

Each city partitions rows into `festivals`, `excluded`, and `pending_review`,
retaining source categories and geographic details. Observed/replacement days,
holiday-extension days, makeup workdays, seasons, clock changes and sporting
events are excluded from festival dates. UK non-London local entries and US
non-New-York state entries are excluded with reasons. Ambiguous US state
exceptions and worldwide/UN/other observances remain pending rather than
silently expanding the confirmed common-observance scope. No deduplication or
runtime query semantics are introduced by this collection.

Validation: `python -m pytest seeds/calendars/2026/test_festivals.py -q`.
Reproducible script (run from repository root):

```powershell
python -m tools.build_festival_calendar --check
python -m tools.build_festival_calendar --output <new-festivals.json>
python -m tools.build_festival_calendar --fetch --root <new-source-directory> --source-suffix YYYYMMDD --output <new-festivals.json>
```

The default rebuild is offline and exactly reproduces the saved classifications,
including pending records. `--check` writes nothing. Output and source files are
never overwritten. Online retrieval raises on HTTP errors or a missing/partial
table; current direct access returned 403, so live retrieval is not verified.
Parser fixtures test both date layouts; the January/December guard is not proof
of exhaustive source coverage. Online snapshots use source-row locators and a
retrieved-body hash; existing rendered-table snapshots retain source-line locators.
Script regression: `python -m pytest tools/test_build_festival_calendar.py -q`.

Pending categories require agreement before inclusion; App interface design
and licensing/distribution suitability of third-party data are not established
by successful extraction.

These are offline authoring sources, not world-state mutations or public Safety
notices. The task contract is `Design/task_specs/safety_planning.md`, section 1.13.

`calendar.json` covers the six current cities. Period ends are inclusive local
dates. `working_day` annotates a make-up workday and is not a holiday exclusion.
`regional_event` is supported for explicitly verified additions; the present
official holiday lists do not claim exhaustive coverage of local festivals.
Singapore replacement Mondays and New York state/federal observance distinctions
are retained in the descriptions, without claiming all businesses close.

`sources/` retains official page text, original/final URLs, retrieval time and a
hash of the retrieved body. A new retrieval uses a new filename. The UK source is
the government's structured JSON; London uses England and Wales, not Scotland.
Shanghai uses the State Council's complete holiday and make-up workday notice.

`safety_time_policy.json` explicitly configures the year, initial before/after
radius, expansion step, regional event count target, local activity/event times,
and source-checked protection attempts before trying another cluster. City
overrides can replace those values. No unspecified value silently falls back to
an LLM. Current initial settings are 10:00-11:30 activity, 08:00-18:00 event envelope,
one day on each side, one-day expansion, 20 distinct notices and two evidenced
protection-conflict attempts. The existing total per-slot attempt limit still
applies. Existing completed clusters retain their original windows where valid.

Source snapshots and normalized dates are reviewed together. To retrieve a new
source without replacing an old snapshot:

```powershell
& 'D:\Miniconda\conda\envs\geoagent\python.exe' -m tools.fetch_safety_calendar_source --url <official-url> --output seeds/calendars/2026/sources/<new-name>.json
```

Read-only preview from the main workspace:

```powershell
& 'D:\Miniconda\conda\envs\geoagent\python.exe' -m tools.preview_safety_time_allocation --world-id world_002 --city-id singapore_singapore --district "Little India"
```

Pass `--batch <existing-batch-directory>` to include that batch's completed
history. The preview does not reserve a seed, allocate a formal task, call an LLM,
request a route, or update any task/world data. Public event visibility remains
the existing batch-bound Safety view; no cross-batch or Dynamic sharing is added.

New runs retain `generation_runs/run_NNNN/safety_time_plan.json`, the append-only
run sequence in `safety_time_allocations.json`, and each completed task's private
`generation_log/time_allocation.json`. Legacy `safety_time_plan.json` at the batch
root is not overwritten. Replaying an old task never replaces its dates.
