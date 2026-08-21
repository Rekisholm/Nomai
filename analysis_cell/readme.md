# Cell-Level Attack Provenance

This directory provides the `analysis_cell` command for offline cell-level
attack backtrace. It consumes database read/write logs and starts from a final
attack request ID (`root_rid`) to produce an interactive HTML graph and a JSON
result file.

## Inputs

Required logs:

- `--read-log`: database read log, one JSON object per line.
- `--write-log`: database write log, one JSON object per line.
- `--root-rid`: final attack request ID used as the backtrace root.

Read log example:

```json
{"time":"2026-01-01T00:00:03Z","event":"select","rid":"A","tbn":"items","pks":["1"],"seq":2,"xmin":123,"xmax":125}
```

Write log example:

```json
{"time":"2026-01-01T00:00:01Z","event":"update","rid":"B","tbn":"items","pk":"1","xid":124,"seq":0,"cells":[{"col":"payload","new":"<script>alert(1)</script>"}]}
```

The write log must include `cells`; old row-level `pks`-only write logs are
rejected because they do not contain column information.

## Basic Run

```bash
python -m analysis_cell \
  --mode basic \
  --read-log runs/<experiment>/<scene>/request_db_read.log \
  --write-log runs/<experiment>/<scene>/request_db_write.log \
  --root-rid <rid> \
  --output-dot runs/<experiment>/<scene>/causal_tree_with_cells.html \
  --output-json runs/<experiment>/<scene>/cell_backtrace.json
```

`--output-dot` keeps the historical option name, but the generated file is an
interactive HTML page.

## Priority Mode

Use `priority` mode to process high-priority cell tasks first and optionally
stop at a task budget.

```bash
python -m analysis_cell \
  --mode priority \
  --budget 100 \
  --request-context-log runs/<experiment>/<scene>/request_context.log \
  --read-log runs/<experiment>/<scene>/request_db_read.log \
  --write-log runs/<experiment>/<scene>/request_db_write.log \
  --root-rid <rid> \
  --output-dot runs/<experiment>/<scene>/causal_tree_with_cells_priority.html \
  --output-json runs/<experiment>/<scene>/cell_backtrace_priority.json
```

Notes:

- Omit `--budget` to exhaust the queue and produce the complete priority trace.
- `--request-context-log` is optional. If omitted, writer diversity falls back
  to request ID.
- `--alpha` and `--beta` control rarity and expansion-cost weights. Defaults
  are `0.5` and `0.5`, and their sum must be `1`.

Evaluate priority budgets for an existing run directory:

```bash
python -m analysis_cell.evaluate_priority \
  --run-dir runs/<experiment>
```

## Structural Pruning Mode

First build `pruning_policy.json` from benign traffic:

```bash
cat runs/<experiment>/<scene>/locust_request_log/*.jsonl \
  > runs/<experiment>/<scene>/normal_request.log

python analysis/pruning_algorithm.py \
  --request-log runs/<experiment>/<scene>/normal_request.log \
  --read-log runs/<experiment>/<scene>/request_db_read.log \
  --write-log runs/<experiment>/<scene>/request_db_write.log \
  --output runs/<experiment>/<scene>/pruning_policy.json \
  --sample-size 128 \
  --trace-depth 4
```

Then run cell backtrace with the policy:

```bash
python -m analysis_cell \
  --mode prune \
  --policy runs/<experiment>/<scene>/pruning_policy.json \
  --read-log runs/<experiment>/<scene>/request_db_read.log \
  --write-log runs/<experiment>/<scene>/request_db_write.log \
  --root-rid <rid> \
  --output-dot runs/<experiment>/<scene>/causal_tree_with_cells_pruned.html \
  --output-json runs/<experiment>/<scene>/cell_backtrace_pruned.json
```

## LLM Modes

`llm` mode asks DeepSeek to keep only cells that look like payload or direct
attack-control data.

```bash
python -m analysis_cell \
  --mode llm \
  --read-log runs/<experiment>/<scene>/request_db_read.log \
  --write-log runs/<experiment>/<scene>/request_db_write.log \
  --root-rid <rid> \
  --output-dot runs/<experiment>/<scene>/causal_tree_with_cells_llm.html \
  --output-json runs/<experiment>/<scene>/cell_backtrace_llm.json
```

Required environment:

- `DEEPSEEK_API_KEY`
- Optional: `DEEPSEEK_MODEL`, default `deepseek-v4-flash`

Combined structural pruning plus LLM:

```bash
python -m analysis_cell \
  --mode prune_llm \
  --policy runs/<experiment>/<scene>/pruning_policy.json \
  --read-log runs/<experiment>/<scene>/request_db_read.log \
  --write-log runs/<experiment>/<scene>/request_db_write.log \
  --root-rid <rid> \
  --output-dot runs/<experiment>/<scene>/causal_tree_with_cells_prune_llm.html \
  --output-json runs/<experiment>/<scene>/cell_backtrace_prune_llm.json
```

Offline smoke test without DeepSeek:

```bash
python -m analysis_cell \
  --mode llm \
  --llm-provider heuristic \
  --read-log runs/<experiment>/<scene>/request_db_read.log \
  --write-log runs/<experiment>/<scene>/request_db_write.log \
  --root-rid <rid>
```

## Outputs

The JSON result includes:

- `links`: retained request-cell-request dependencies.
- `request_ids`: request IDs supported by certain dependencies.
- `candidate_request_ids`: request IDs including uncertain candidates.
- `pruned_cells`: cells removed by pruning or LLM modes.
- `unresolved_cells`: cells that could not be resolved from the logs.
- `priority_search`: priority-mode budget, processed tasks, and pending tasks.

The HTML output is the easiest way to inspect the graph interactively.

## Tests

```bash
python -m unittest analysis_cell.test_cell_backtrace
```
