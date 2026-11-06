# Tasks & Evaluation migration

World Studio links to the complete workbench at `/tasks/`. Start the normal World Studio server, then select Tasks & Evaluation.

Task and batch browsing, task details, attachments, author maps and routes, task editing/review, the existing App explorer endpoints, and Test Results display/execution endpoints are connected. Other interface contracts are registered and return an explicit not-connected response until their existing backend is migrated and tested.

The bundled task records include a dynamic episode exported from the verified target-first generation flow. They are browsing excerpts, not complete evaluation batches. Read `data/manifests/workbench_samples.json` for source records and hashes.

When runtime batches exist under `outputs/benchmarks`, the workbench reads them. Otherwise it reads `examples/workbench/benchmarks`. The `world` query parameter chooses the saved task world. For the bundled samples, use `/tasks/?world=world_002`.

World registry contents and frozen world files are not copied for browsing. Saved task maps and author evidence remain in the bundles. Executing evaluation requires the exact provenance-bound assets and hash validation.

| Method | Interface | Feature | Backend status |
|---|---|---|---|
| GET | /api/app-content/catalog | app | Connected |
| GET | /api/app-content/event-overview | app | Connected |
| GET | /api/app-content/map-query | app | Connected |
| GET | /api/app-content/preview | app | Connected |
| GET | /api/app-content/query | app | Connected |
| GET | /api/app-content/static-events | app | Connected |
| GET | /api/asset | browse | Connected |
| GET | /api/batches | browse | Connected |
| GET | /api/cached_places | map | Connected |
| GET | /api/cities | browse | Connected |
| GET | /api/config | browse | Connected |
| POST | /api/dynamic/confirm | generation | Pending migration and integration |
| GET | /api/dynamic/cost-actual | generation | Pending migration and integration |
| GET | /api/dynamic/overview | generation | Pending migration and integration |
| GET | /api/dynamic/sources | generation | Pending migration and integration |
| POST | /api/evidence/update | generation | Pending migration and integration |
| POST | /api/generate | generation | Connected (Grounding) |
| POST | /api/generate/cancel | generation | Connected (Grounding) |
| GET | /api/generate/status | generation | Connected (Grounding) |
| POST | /api/itinerary/cost-preflight | generation | Pending migration and integration |
| GET | /api/itinerary/route-resources | generation | Pending migration and integration |
| GET | /api/kb | generation | Pending migration and integration |
| POST | /api/kb/approve | generation | Pending migration and integration |
| GET | /api/kb/cities | generation | Pending migration and integration |
| POST | /api/kb/reject | generation | Pending migration and integration |
| POST | /api/kb/update | generation | Pending migration and integration |
| GET | /api/manifest | browse | Connected |
| POST | /api/multi-entity/cost-preflight | generation | Pending migration and integration |
| GET | /api/multi-entity/route-resources | generation | Pending migration and integration |
| POST | /api/region/build/cancel | generation | Pending migration and integration |
| GET | /api/region/build/config | generation | Pending migration and integration |
| POST | /api/region/build/start | generation | Pending migration and integration |
| GET | /api/region/build/status | generation | Pending migration and integration |
| POST | /api/region/cost-preflight | generation | Pending migration and integration |
| GET | /api/region/route-resources | generation | Pending migration and integration |
| GET | /api/region/rule-cities | generation | Pending migration and integration |
| GET | /api/region/rules | generation | Pending migration and integration |
| POST | /api/region/rules/update | generation | Pending migration and integration |
| GET | /api/region/service-detail/preflight | generation | Pending migration and integration |
| POST | /api/region/service-detail/start | generation | Pending migration and integration |
| GET | /api/region/service-detail/status | generation | Pending migration and integration |
| POST | /api/route-planning/cost-preflight | generation | Pending migration and integration |
| GET | /api/route-planning/resources | generation | Pending migration and integration |
| GET | /api/route_cache | map | Connected |
| GET | /api/routes | map | Connected |
| DELETE | /api/run | evaluation | Connected |
| GET | /api/run | evaluation | Connected |
| POST | /api/run/start | evaluation | Connected |
| GET | /api/run/status | evaluation | Connected |
| GET | /api/run/task | evaluation | Connected |
| GET | /api/runs | evaluation | Connected |
| POST | /api/safety/cost-preflight | generation | Pending migration and integration |
| GET | /api/safety/route-resources | generation | Pending migration and integration |
| GET | /api/task | browse | Connected |
| GET | /api/task/files | browse | Connected |
| POST | /api/task/judge | review | Connected |
| POST | /api/task/review | review | Connected |
| POST | /api/task/save | review | Connected |
| POST | /api/task/source | review | Connected |
| GET | /api/task_types | browse | Connected |
| GET | /api/world/full | map | Connected |
| GET | /api/world/points | map | Connected |
