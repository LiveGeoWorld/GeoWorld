# Generate Grounding tasks

Grounding tasks ask an agent to identify a place from a street-view image and
geographic clues. The generator uses a registered city version, its places and
participants, Google Street View, and a vision-capable writing model.

## Configure and run

Follow the [configuration guide](world_construction.md) to configure Google Maps
and a vision model marked for task generation. The writer uses the provider's
Chat Completions endpoint. Register a city in World Studio, then open
**Tasks & Evaluation**, select **Grounding** and that city, choose a model and
start with one task. Progress, cancellation and appending to a batch use the
same workbench controls.

Alternatively, from the repository root:

```sh
python -m task_module.generator --world-id YOUR_WORLD --city YOUR_CITY --types place_grounding --counts 1 --stage-model YOUR_ENDPOINT --new-batch
```

Only Grounding construction is connected here. Other task-generation interfaces
are marked **Pending migration and integration**; their browsing and evaluation
interfaces remain available.

## Construction and review

The generator samples a target using the existing seeded strategy, gathers map
evidence and Street View imagery, and passes supported evidence to the writer.
The writer sees the final task image first and the other available reference
views. With the default four headings, this is one final image and three
authoring references. The evaluated agent receives only the final image.

The English writer prompt and retained Chinese source are under
`task_module/task_types/place_grounding/batch_generation/prompts/`.
The original bounded retries, quality checks, deterministic fallback and
formalization rules are preserved. Check the saved generation method and
validation report when reviewing a task.

Generated batches are saved under `outputs/benchmarks/place_grounding/`.
Each task keeps its local image, source evidence, reference answer and validation
records, with provenance bound to its city version. API use is recorded with
the batch. Review the request and image in the workbench before approving it.

## Evaluation

Open **Test Results**, select the generated batch and model, and start an
evaluation. The runner loads the task's frozen assets and final image, exposes
the existing public Apps, and saves the model's answer, tool calls and Judge
result. Grounding requires selecting a returned map candidate before submitting
the final answer. See the [evaluation guide](evaluation.md).
