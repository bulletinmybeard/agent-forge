# Model catalog

The catalog exists to compare models across providers and find alternatives.
The case it is built for: you run a model on one provider and want one that is as capable on another, because of price, availability, or moving off a provider entirely.
`POST /api/model-catalog/equivalents` takes a source provider + model and ranks the closest matches on one or more of the other providers.

To support that, AgentForge keeps a normalized catalog of the models each backend offers (DeepInfra, OpenRouter, Ollama). It also serves that catalog read-only over REST, so you can browse and filter it directly.

Two API groups sit on `agentforge-web` (`:8200`):

- `/api/model-catalog/*` finds equivalent models across providers. This is the reason the catalog exists.
- `/api/catalog/*` browses and filters the raw model metadata per provider.

Every provider's entries are normalized to one `UnifiedModel` shape, so models from different providers can be compared field for field.

## Data files

The catalog is built from JSON dumps under `data/catalogs/`:

| File                     | Provider   | Top-level shape               |
| ------------------------ | ---------- | ----------------------------- |
| `deepinfra-models.json`  | DeepInfra  | bare array                    |
| `openrouter-models.json` | OpenRouter | object, models under `data`   |
| `ollama-models.json`     | Ollama     | object, models under `models` |

These files are gitignored.
They are large, provider-sourced, and change often, so the operator supplies them rather than committing them.
The loader looks for each file in `data/catalogs/` (local dev), then `/app/data/catalogs/` (container), and falls back to the repo root.
Override the directory with `AGENTFORGE_CATALOG_DIR`.

Each raw entry is normalized to a `UnifiedModel` with these fields:

`id`, `provider`, `model_id`, `name`, `family`, `description`, `type`, `context_length`, `max_tokens`, `pricing` (input/output per 1M tokens), `capabilities` (canonical tokens), `tags`, `parameter_size`, `deprecated`, `is_cloud`, `last_updated`, and `raw` (the original provider entry).

## Endpoints

All on `agentforge-web` (`:8200`). `{provider}` is one of `deepinfra`, `openrouter`, `ollama`.

| Method | Path                                 | Purpose                                           |
| ------ | ------------------------------------ | ------------------------------------------------- |
| GET    | `/api/catalog/providers`             | List providers with model counts and cache state. |
| GET    | `/api/catalog/{provider}`            | List a provider's models. Filterable (see below). |
| GET    | `/api/catalog/{provider}/{model_id}` | One model. The `model_id` may contain slashes.    |
| GET    | `/api/model-catalog/providers`       | The three provider names.                         |
| POST   | `/api/model-catalog/equivalents`     | Rank equivalent models on other providers.        |

### Filtering a provider's models

`GET /api/catalog/{provider}` takes these query params, AND-combined: `family`, `capability`, `model_type`, `parameter_size`, `min_context_length`, `deprecated` (`false` | `true` | `any`, default `false`), `is_cloud` (`true` | `false` | `any`), and `limit`.

```bash
# Non-deprecated DeepInfra models with at least a 128k context
curl "http://localhost:8200/api/catalog/deepinfra?min_context_length=128000"

# One model by id (slashes in the id are fine)
curl "http://localhost:8200/api/catalog/deepinfra/Qwen/Qwen3-235B-A22B"
```

### Finding equivalents

`POST /api/model-catalog/equivalents` takes a source model and ranks comparable models on the other providers.
It is computed at query time by an LLM (the `agent-heavy` profile), not from a static mapping.
There are no learned equivalences: the endpoint builds a compact dossier for each candidate (family, size, type, capabilities, context, pricing) and asks the model to rank them, returning a score and a short reasoning per candidate.

```bash
curl -X POST "http://localhost:8200/api/model-catalog/equivalents" \
  -H 'content-type: application/json' \
  -d '{"source": {"provider": "deepinfra", "model_id": "Qwen/Qwen3-235B-A22B"}}'
```

Body:

- `source`: `{provider, model_id}` (required).
- `targets`: optional list of providers to compare against (default: all except the source).
- `max_results_per_target`: how many ranked candidates per provider (default 5).
- `?force=true`: bypass the cache.

The response carries the resolved source model plus, per target provider, a ranked list of `{model, score, reasoning}`.

## Caching

Catalogs and equivalence results are cached in Redis, fail-soft: if Redis is down, the loader reads the JSON directly.

- `catalog:<provider>` (`catalog:deepinfra`, `catalog:openrouter`, `catalog:ollama`): the normalized model list. TTL `AGENTFORGE_CATALOG_TTL`, default `3600` (one hour).
- `equiv:<hash>`: a cached equivalence result, keyed by the request body. TTL five minutes.

## Deploying catalogs (`--with-catalog`)

The catalog JSONs are not baked into the Docker image. They are gitignored and bind-mounted through `./data`, so they have to reach the remote box separately.
Run the deploy with `--with-catalog`:

```bash
scripts/deploy-remote.sh --with-catalog
```

It rsyncs `data/catalogs/*.json` to `<remote>/data/catalogs/`, then deletes the `catalog:deepinfra`, `catalog:openrouter`, and `catalog:ollama` Redis keys so the new files are reloaded on the next request instead of waiting out the one-hour TTL.
The sync runs on both config-only and full deploys. Without the flag, the catalogs on the box are left as-is.

## See also

- [api.md](api.md): the `/api/catalog/*` + `/api/model-catalog/*` endpoints this draws on.
- [README.md](README.md): the full docs index.
