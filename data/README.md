# Demo data

| Directory | Purpose | Included data |
| --- | --- | --- |
| `examples/world_studio/` | Start World Studio without provider credentials | Two recorded London areas, three places, six cached route entries, three service records and three recorded participants |
| `examples/workbench/` | Browse Tasks & Evaluation | Recorded task prompts, candidate evidence, image attachments and a verified dynamic event chain |
| `data/manifests/` | Identify the bundled examples | Source descriptions, file hashes and display-translation records |

The task examples include the files and images used by the browser. Grounding and Discovery use saved outputs from their current generation contracts. The dynamic example follows a group dinner request through a requirement update and two temporary restaurant closures, retaining one target and two distractors before event replay.

English display translations accompany source tasks where necessary. These examples are for browsing. Running evaluation additionally requires the original version-bound world assets and hash validation.

Runtime outputs are created under `outputs/`. Wikivoyage source downloads are stored under `data/raw/` when a user builds a city. Neither directory is bundled with the startup examples. `python -m vis.demo` installs only the packaged `world_demo` registry into `outputs/worlds/` on first launch; subsequent launches reuse it without overwriting changes.

`examples/app_runtime/benchmarks` contains the small, complete App input records.
It uses the normal benchmark layout and common runtime loader. It is kept apart
from task-review excerpts so incomplete paper examples are not used as runtimes.

Recorded evaluation excerpts live in `examples/test_results/`. Their source hashes
and exact task matches are listed in `data/manifests/result_samples.json`. The
normal demo launcher installs them into the ordinary result store without
overwriting existing user runs.
