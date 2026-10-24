# contract/
The only package both subteams depend on. **Frozen at hour 2.** Changing it requires bumping `SCHEMA_VERSION` and both teams agreeing.

| File | Purpose |
|---|---|
| `schema.py` | TypedDicts for the observation (input) and action (output), plus the command constants (plan §6) |
| `examples/` | Example JSON files for building the advisor without running the simulator |
