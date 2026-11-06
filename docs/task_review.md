# Task review

Open **Tasks & Evaluation → Task Review** to inspect a task, its attachments and saved construction evidence. The task examples and recorded test results are separate: reviewing a task does not rerun its evaluation.

## Edit and review

Use the task's existing editor to change supported fields, then save. Field permissions remain task-specific. Grounding and Discovery expose prompt editing while keeping reference answers and Judge evidence read-only in their review pages. Saving a task marks it as needing review again. Approve, reject or add review notes using the review controls; the same batch review record is used by the evaluation selector.

The legacy source and Judge editors use their existing save interfaces. Bundled curated sources have local, portable copies under `examples/workbench/sources`. Synchronization writes to those copies inside this project, rather than a source path on another computer. These are authoring operations, not public agent tools.

## Maps and routes

The author map endpoints read the registered world's saved map and route records. Task-specific whole-city markers require that task's original registered city version and matching frozen map. A missing or changed binding is reported rather than replaced with another city's map. Task-local preview points remain available in the saved task bundle.

Route viewing reads the existing cache; it does not call a map provider or fill missing routes. Use the saved origin, destination and mode filters to inspect recorded routes.

## Data and reproducibility

Work on a separate copy when experimenting with edits. Saving a prompt does not translate the underlying source task automatically. A display translation applies only while it still corresponds to the saved source prompt; an old translation must not hide a new edit. Saved sandbox display projections likewise must not override a changed sandbox.

Task generation and its type-specific configuration are handled separately from reviewing existing tasks. The interface contract list is maintained in `vis/tasks/interfaces.json`.
