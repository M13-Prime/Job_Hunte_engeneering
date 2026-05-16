# Contributing

Thanks for your interest! This project is single-maintainer right now, but PRs
that match the philosophy below are welcome.

## Philosophy

- **One LLM provider abstraction.** All LLM calls go through `litellm` via
  `signal_tracker/classifier/llm.py::classify`. Never import a
  provider-specific SDK (`anthropic`, `openai`, ...) elsewhere.
- **Strict typing.** `mypy --strict` must stay green. New code is annotated.
- **Each new collector** inherits `BaseCollector` and yields `CollectedItem`
  via `async def collect(self)`. Cache HTTP for 6h via `FileCache`.
- **No silent failures.** Network or LLM errors are logged with `extra={...}`
  key/value pairs (see `utils/logging.py`).
- **Tests over mocks-of-mocks.** Use `httpx.MockTransport` for HTTP and an
  `unittest.mock.AsyncMock` for `litellm.acompletion`. Add a
  `@pytest.mark.live` test if you want a real call.

## Local dev

```bash
make install         # uv sync --extra dev
make doctor          # check .env / DB / LLM connectivity
make lint            # ruff
make typecheck       # mypy --strict
make test            # pytest (skip live)
```

## Branches & commits

- One branch per topic, named `feature/<short-name>` or `fix/<short-name>`.
- Commit messages: imperative mood ("Add X", "Fix Y"), explain *why* in the body.
- Squash trivial fixups locally before pushing.

## Adding a collector

1. Drop a new file under `src/signal_tracker/collectors/` extending
   `BaseCollector`.
2. Add an entry under the matching section in `config/sources.yaml`
   (gated on `enabled: true`).
3. Wire it inside `pipeline.build_default_collectors`.
4. Add a test file under `tests/test_<name>_collector.py` using
   `httpx.MockTransport`.

## Adding a new signal type

1. Append the literal to `SignalType` in `classifier/schemas.py`.
2. Document it in `CLASSIFIER_PROMPT_V1` (taxonomy section) and add a
   few-shot example.
3. Bump `CLASSIFIER_PROMPT_VERSION` if the change is semantic.

## Security / data

- Never commit a real `.env`. The example template is `.env.example`.
- Don't log API keys. Use `extra={"redacted": True}` if a value is sensitive.
- Don't scrape sites with restrictive ToS. LinkedIn is a hard "no".
