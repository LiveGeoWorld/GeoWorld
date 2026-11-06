# GeoWorld

English · [简体中文](README.zh-CN.md)

**Build evolving city environments for geographic agents.**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#quick-start)

**🌐 Project Website** · **📄 Paper** · **🗂️ LiveGeo Dataset** · [**📚 Documentation**](#documentation)

<!-- Link the project website, paper, and dataset when their official URLs are available. -->

![Overview of GeoWorld and LiveGeo, showing the shared city timeline, tools, and task categories](docs/assets/geoworld-livegeo-overview.png)

## Overview

**GeoWorld** is a geographic simulation platform. You can build or load a city, let agents query places, routes, and local services, and observe how they respond to changes in traffic, weather, and opening hours.

**LiveGeo** is the benchmark we built with GeoWorld. It covers seven categories of geographic tasks and compares how agents make decisions in static and dynamic environments.

- [Try GeoWorld](#quick-start) — Open the sample city in World Studio.
- [Evaluate with LiveGeo](#run-livegeo) — Test your agent on the benchmark tasks.
- [Generate your own data](#build-your-own-benchmark) — Build a task generator using GeoWorld's cities, tools, and event replay.

## Quick Start

With Python 3.10 or later, run these commands from the repository root:

```sh
python -m pip install -e .
python -m vis.demo --port 8769
```

Once the server starts, enter `http://localhost:8769/` in your browser to explore the example map, view the character graph, or open the world manager. The bundled example contains three recorded London places, their cached routes and service records, and three recorded participants. Starting it registers its own small world and requires no API credentials. The browser fetches basemap tiles online. Tasks & Evaluation includes recorded task examples for trying the workbench.

To create a city from geographic sources, configure your providers, create an empty world in World Studio, and choose a city or import a city seed. The [world construction guide](docs/world_construction.md) explains configuration and city registration.

## Run LiveGeo

LiveGeo contains **1,088 static tasks and 150 matched dynamic tasks** across seven categories.

| Task | What the agent does |
| --- | --- |
| Grounding | Identify a place using visual or textual clues. |
| Discovery | Find a place that meets the user's location, service, or preference requirements. |
| Mobility Planning | Determine whether a trip is feasible and provide a route when required. |
| Multi-Entity | Find a common plan for people with different starting points and schedules. |
| Long-Horizon | Plan a sequence of activities that fit together across one or more days. |
| Safety-Aware | Account for risks that affect a destination or the route to it. |
| Region-Aware | Apply local practices and geographic context when choosing a place. |

Matched cases start with the same request and initial world state. **Static** keeps conditions fixed. **Task-Only Dynamics** introduces the events authored for that task. **Shared Dynamics** adds other city events to the same timeline, so agents must work out which changes affect their plans.

Browse recorded model answers, Judge results and tool calls in **Tasks & Evaluation → Test Results**. The bundled examples need no model credentials. To run your own evaluations, see the [evaluation guide](docs/evaluation.md).

## Build Your Own Benchmark

You can write your own task generator on top of GeoWorld. Your generator decides which situations to create and what makes an answer valid. GeoWorld provides the city, the tools agents use to inspect it, and a clock for replaying changes.

1. **Pick a city.** Reuse a saved city snapshot or build one in World Studio. Record the version used to generate your tasks so they can be run against the same places, routes, and services.
2. **Generate requests and reference answers.** Use facts from that city to choose a scenario and write a user request. Save the requirements and the evidence that supports the reference answer for evaluation. Give the agent the request and the tools it needs to solve it.
3. **Schedule changes.** For a dynamic task, specify when a change happens, what it affects, and where an agent can learn about it. Check that the reference answers satisfy the requirements before and after the change.
4. **Run and score.** Load the task with its city snapshot and event schedule. Let the agent interact with the environment, then score its submission using the rules for your task.

For example, you could write a generator for café selection. Have it find cafés that are open at the requested time and reachable within the user's walking limit. Choose and verify a reference answer, then schedule a temporary closure and check which alternatives still work. You can run the same initial request with and without that event to see whether agents notice the change and select a viable alternative. The LiveGeo pipeline provides a worked example of how to construct and verify this kind of comparison.

## Data and Reproducibility

| Data | What it contains |
| --- | --- |
| **GeoWorld city snapshots** | The geographic and service data used by the simulator. The paper's instantiation covers six cities, 97 regions, 1,376 points of interest, and 106,772 cached route records. |
| **LiveGeo benchmark data** | Task requests, their associated city versions, scheduled events, and the reference material used for evaluation. |
| **World Studio example** | A small synthetic map and character file for trying the interface locally. |

To reproduce a run, use the same code and dataset versions, city snapshot, evaluation setting, and agent and model configuration. The [data guide](data/README.md) explains where the example and local world outputs live.

## Repository Structure

| Directory | Contents |
| --- | --- |
| `world_module/` | City maps, characters, regional information, and world version management. |
| `vis/` | The World Studio web interface and local server. |
| `task_module/` | Task construction. |
| `app/` and `test_runner/` | Agent tools, environment execution, and evaluation. |
| `shared/` | Shared configuration, provider clients, and utilities. |
| `seeds/`, `examples/`, and `docs/` | City seeds, the runnable example, and usage guides. |

## Tests

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

Unit tests use temporary data and the bundled configuration example. They do not require API credentials or download the Wikivoyage source package.

## Documentation

- [Generate Grounding tasks](docs/grounding.md)
- [Task review](docs/task_review.md)
- [Architecture](docs/architecture.md)
- [World construction and configuration](docs/world_construction.md)
- [Data organization](data/README.md)

## Citation and License

If you use GeoWorld or LiveGeo in your research, please cite *LiveGeo: Benchmarking Geographic Agents in Continuously Evolving Worlds*.

<!-- Add the verified BibTeX and links to the code license, dataset license, and third-party asset notices. -->
