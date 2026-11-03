# free.ai

free.ai is OpenAI-compatible in **payload shape** but not in **paths**. AI
Switch hides that difference behind a small wire profile, so a key from
[free.ai](https://free.ai) behaves like any other upstream: add it, health-check
it, and route to it through the hub.

## Add it

1. **Vendors → Add vendor**
2. Provider: **Free.ai** (base URL fills in as `https://api.free.ai/v1`)
3. Paste the key (`sk-free-…`) and save.

Use the **API root** (`https://api.free.ai/v1`), not the chat endpoint. If you
paste `https://api.free.ai/v1/chat` or `…/v1/chat/completions`, the URL is
reduced to the root on save — otherwise the probe would ask for
`/v1/chat/v1/models`.

Health check probes a self-hosted model, so it stays inside the free daily
allowance instead of spending premium tokens.

## What differs from plain OpenAI

| Operation | OpenAI path | free.ai path | Handled by |
| --- | --- | --- | --- |
| Chat | `POST /v1/chat/completions` | `POST /v1/chat/` | probe + proxy rewrite |
| Image | `POST /v1/images/generations` | `POST /v1/image/generate/` | probe + proxy rewrite + response reshape |
| Model list | `GET /v1/models` → `{"data":[…]}` | `GET /v1/models` → `{"models":[…]}` | discovery adapter |
| Responses API | `POST /v1/responses` | not implemented (404) | endpoint list narrowed to chat |

The model inventory carries ~460 entries across many modalities. AI Switch keeps
only the types it can route — `chat` and `image` — and sorts self-hosted (free)
models first.

## Routing through the hub

Clients keep speaking OpenAI. Point them at AI Switch with a downstream key and
the proxy translates the path on the way out:

```bash
curl -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer <downstream key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen7b","messages":[{"role":"user","content":"Hello!"}]}'
```

Images are answered in OpenAI's shape even though free.ai replies with
`{job_id, url, image_url, share_url}`:

```bash
curl -X POST http://127.0.0.1:8787/v1/images/generations \
  -H "Authorization: Bearer <downstream key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"sdxl","prompt":"a sunset over mountains"}'
# -> {"created": 1793694887, "data": [{"url": "https://api.free.ai/static/outputs/….png"}]}
```

## Free models

The self-hosted tier costs zero tokens within the daily allowance and is what
health checks use:

- `qwen7b`
- `qwen3-8b`
- `mistral`
- `deepseek-r1`

Everything else in the inventory (`openai/…`, `moonshotai/…`, `premium/…`) draws
on paid tokens.

## Not routed

Video, TTS and translate are **not** exposed through the OpenAI-compatible
proxy. Their native contracts do not map onto OpenAI's:

- `/v1/video/generate/`, `/v1/tts/` answer with a job URL rather than media bytes
- `/v1/translate/` takes `{"text","target"}`, which has no OpenAI equivalent
  (`/v1/audio/translations` is a multipart audio upload)

Calling them through the hub would return a body clients cannot read, so they
are deliberately left out. Use them directly against `api.free.ai` if needed.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `HTTP 404 on …/v1/chat/v1/models` | The base URL was the chat endpoint (`https://api.free.ai/v1/chat`) instead of the API root (`https://api.free.ai/v1`). Pasted endpoint URLs are now reduced to the root on save; re-save an existing vendor to clean it up. |
| `404 … invalid response shape` on model scan | Vendor points at a proxy that rewrites `/v1/models` into another shape; use the native base URL. |
| Chat 404 with no error body | The upstream base URL is missing `/v1`, so the rewrite produced `/chat/` at the root. |
| Image 404 `No healthy upstream (image)` | The `sdxl`-style model was never discovered — re-run the health check. |
| Health check slow | First scan walks the whole inventory; later runs reuse it. |
