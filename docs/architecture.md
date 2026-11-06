# Architecture

| Module | Responsibility |
|---|---|
| `world_module` | World registry, map and character construction, services and cultural source assets. |
| `app` | Public tool contracts, sandbox Apps, tool dispatch, virtual time, events and frozen-asset readers. |
| `vis` | World Studio, Tasks & Evaluation, HTTP adapters and author review projections. |
| `shared` | Configuration, clients, geographic helpers and browser assets. |
| `task_module` | Task construction. |
| `test_runner` | Agent execution and evaluation. |

The App runtime reuses the benchmark implementations. The workbench reads the
same active world registration as World Studio. Author event/protection overlays
are distinct from the public observations returned to an agent.

`app/runtime/episode.py` owns runtime assembly. The Runner retains model execution
and private Judge work; the author workbench only reads public App results from
the same assembly. Demo and full records use identical input formats.

`vis/tasks/evaluation_http.py` mounts the existing execution and result endpoints.
`test_runner` owns Agents, Judge checks and recovery; it uses the common `app`
runtime. Private verification helpers are relocated without generator entry points.

`task_module` owns Grounding construction and the shared task formalization
contract. Runner verification imports this contract through compatibility
modules, so generation and execution use the same validation implementation.
`vis/tasks/generation_http.py` mounts the existing background build lifecycle.
