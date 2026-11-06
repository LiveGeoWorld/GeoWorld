# Demo data

| Directory | Purpose | Included data |
| --- | --- | --- |
| `examples/world_studio/` | Start World Studio without provider credentials | Two recorded London areas, three places, six cached route entries, three service records and three recorded participants |
| `examples/workbench/` | Browse Tasks & Evaluation | Recorded task prompts, candidate evidence, image attachments and a verified dynamic event chain |
| `examples/test_results/` | Inspect real saved model runs | Two task results in one display run, matched to the reviewed tasks; original answers, scores and trajectories |
| `data/manifests/` | Identify the bundled examples | Source descriptions, file hashes and display-translation records |

The task examples include the files and images used by the browser. Grounding and Discovery use saved outputs from their current generation contracts. The dynamic example follows a group dinner request through a requirement update and two temporary restaurant closures, retaining one target and two distractors before event replay.

English display translations accompany source tasks where necessary. These examples are for browsing. Running evaluation additionally requires the original version-bound world assets and hash validation.

Runtime outputs are created under `outputs/`. Wikivoyage source downloads are stored under `data/raw/` when a user builds a city. Neither directory is bundled with the startup examples. `python -m vis.demo` installs the packaged `world_demo` registry and recorded result excerpts into `outputs/worlds/` on first launch; subsequent launches reuse it without overwriting changes.

## App runtime example

The bundled London example uses the normal task directory, participant snapshot,
world provenance, event ledger and time-context formats. It contains a static
initial record and its dynamic record, both read by the same episode loader as
formal test execution. Calendar and Messaging show the saved participant state.
The two recorded dynamic events update a dietary preference and close a merchant.
Their English display copies retain the source timestamps and effects. Source
hashes and translations are listed in `data/manifests/app_demo.json`.

In APP, choose Static or Dynamic, then the task runtime and event checkpoint.
A sole matching example is selected automatically; multiple records remain an
explicit choice. Checkpoint changes replay fresh state and preserve the query
inputs. App reads do not submit plans, send messages or write to the source data.
This is an App input excerpt, not a generated evaluation task with a reference
answer. The recorded task examples for paper/task review remain separate files.

`examples/app_runtime/benchmarks` contains the small, complete App input records.
It uses the normal benchmark layout and common runtime loader. It is kept apart
from task-review excerpts so incomplete paper examples are not used as runtimes.
