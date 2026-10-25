# tests/fixtures/sources/

Sanitized AIS inputs for Person 1's tests. All MMSIs are fake (`999…`).

| File | Purpose |
|---|---|
| `aisstream_messages.fixture.jsonl` | One AISStream-shaped frame per line, `{"case", "message"}`: valid Class A/B, sentinels, `Valid=false`, unavailable position, outside the bbox, static types 5 and 24, an unusable static message, a provider error frame, an unsupported type |
| `capture.fixture.jsonl` | Normalized `AISRecord`s (provider `SYNTHETIC_FIXTURE`) every 10 s along the recipe route at 18 kn, plus one static record. `prepare_routes` turns it into one route offline |
