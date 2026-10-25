# runs/

Generated run artifacts. Everything here except this README is git-ignored.

`run_comparison.py` and the dashboard's **Save run artifacts** button write one directory per paired comparison: `runs/<scenario>-s<seed>-<inputs hash>/`. A `-2`, `-3`, … suffix is added if the directory already exists.

| File | Content |
|---|---|
| `inputs.json` | The frozen `RunInputs`: route bundle, land map, scenario and config. Rerunning these inputs offline reproduces the results |
| `result_no_action.json`, `result_advisor.json` | `RunResult` per policy: manifest (source/map/config hashes, seed, weather, `fixture_only`, status), summary, frames, events and decisions. `--no-frames` drops the frames |
| `comparison.json` | `compare_runs` output: paired differences (null when either side is null), per-voyage pairs, pairing check, blocked-zone violations, rejected candidates by factor, notes |

A run built from `SYNTHETIC_FIXTURE` routes or fixture land has `fixture_only: true`. Such a run demonstrates the code; it is not real-data acceptance evidence.
