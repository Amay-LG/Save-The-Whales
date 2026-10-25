# Run artifacts

The comparison harness writes `comparison.json` and `manifest.json` into unique
UTC timestamp + random suffix directories. Inputs remain frozen per run; results
record source/map/config hashes, seed, policy, fixture flag, runtime, and status.
Frames/events/decisions are supplied by the bounded simulator. No credentials,
raw AIS subscription payloads, or database are written. Generated run folders
are ignored by the shared repository rules. Keep failures/incomplete results.
