# Public Apps

GeoWorld provides MapApp, LocalServiceApp, CalendarApp, MessagingApp,
WeatherApp, CityNewsApp and UserInterfaceApp.

- The top-level modules describe their existing public tool contracts.
- `apps/` implements the sandbox Apps used by the benchmark.
- `runtime/` dispatches tools and maintains virtual time and event state.
- `assets/` reads bound snapshots, event geometry and frozen calendar data.

The World Studio **Tasks & Evaluation → APP** page browses public world facts
and existing author event views. It does not run an agent or submit an answer.
Select a registered world in World Manager to inspect its versioned assets.
Each query uses the owning App's existing public method. Internal place and route
IDs, task candidate membership and reference answers are not public tool results.

Task-level previews require the exact frozen assets recorded in provenance.
Missing assets or inconsistent hashes remain errors, rather than falling back
to task excerpts. Task construction and evaluation execution are separate from
this runtime.

For an immediately queryable example, run `python -m vis.demo`. It registers the
bundled three-place London world and opens it for both World Studio and APP.
Recorded transit-unavailable results remain unavailable; no live route is invented.

`runtime/episode.py` assembles the same frozen inputs, Apps, event pool and decision
window used by the formal Runner. APP queries and read-only previews call this
loader. They do not use a separate demo implementation. Use the task runtime
and checkpoint controls to inspect Calendar, Messaging and dynamic App state.
