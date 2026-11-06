# World construction

Run commands from the repository root. The map and character command entry
points are python -m world_module.map and python -m world_module.character.cli.
Use --help to inspect their inputs. The integrated world job entry point is
python -m vis.run_formal_world_build.

Copy config.yaml.example to config.yaml for external services. Configure your
own Maps and model providers as required by the job. The sample viewer uses a
local synthetic world and does not require these providers.

New city registration requires compatible pre-extracted Wikivoyage evidence
for the city or a local English Wikivoyage dump at
`data/raw/wikivoyage/enwikivoyage/enwikivoyage-latest-pages-articles.xml.bz2`.
The demo viewer loads its JSON directly. The demo is not a registered
benchmark world. Provider-backed jobs require a configured Maps or model
endpoint for their selected steps.

Run `python tools/fetch_wikivoyage.py` to fetch and verify the official dump.

Registration reuses local evidence or the local dump. If both are absent, it downloads and verifies the official dump before extracting city evidence. The prefetch command is optional.
