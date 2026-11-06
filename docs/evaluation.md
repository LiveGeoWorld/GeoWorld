# Test Results

Open **Tasks & Evaluation → Test Results** in World Studio. The bundled recorded examples work without a model API key.

## Explore recorded runs

Select a run to inspect its task, model answer, saved Judge checks, token use and tool-call trajectory. The bundled display run contains two real results matched to the task-review examples: Region-Aware and Safety-Aware. Each result retains its original source run; they were not executed together. Counts describe these excerpts, not the complete source batches. Answers and scores retain their original values. Source hashes are recorded in `data/manifests/result_samples.json`.

Grounding, Discovery and the new dynamic task have review examples but no matching bundled model results. They are not assigned invented scores. Viewing saved results does not call a model or re-run the Judge.

## Run an evaluation

Configure a testing model in a local `config.yaml` using `config.yaml.example`. Load tasks with their original provenance-bound city versions and frozen assets. In Test Results, choose the task type, city, batch and model, then start the evaluation. The existing Runner handles tool calls, submissions and Judge checks; progress and results are saved under `outputs/evaluations`.

The small task-review excerpts are not a substitute for the complete frozen assets required to execute a benchmark. The App startup example remains usable without those benchmark assets.

Single-task and batch execution are also available from the repository root:

```bash
python -m test_runner.runner --task /path/to/task --model MODEL_KEY
python -m test_runner.runner --tasks /path/to/tasks --model MODEL_KEY
```

Use `--world` and `--routes` only where supported by the task's existing input contract. Dynamic execution retains `--dynamic-event-mode task_only` or `shared_world`.

## Resume and manage runs

Completed runs can be deleted in the workbench. Active runs cannot be deleted. The existing recovery commands retain their recorded plan and audit formats:

```bash
python -m test_runner.recover_task --help
python -m test_runner.recover_batch --help
```

Do not edit the source task, reference answer or frozen-asset hashes to resume a run. Runtime outputs, credentials and local acceptance logs are not bundled with the examples.
