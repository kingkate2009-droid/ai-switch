# Volcengine Ark (火山方舟) setup

Volcengine Ark looks like a single OpenAI-compatible provider but is actually
two products behind one hostname. They differ in **base URL, API key and model
names**, and mixing them up is expensive rather than merely broken: the Coding
Plan is a prepaid subscription, while `/api/v3` bills pay-as-you-go.

AI Switch therefore ships four presets instead of one.

| Preset | Base URL | Protocol | For |
|---|---|---|---|
| `volcengine` | `https://ark.cn-beijing.volces.com/api/coding/v3` | OpenAI | Coding Plan, OpenAI-compatible tools |
| `volcengine-coding-anthropic` | `https://ark.cn-beijing.volces.com/api/coding` | Anthropic | Coding Plan, Claude Code |
| `volcengine-ark` | `https://ark.cn-beijing.volces.com/api/v3` | OpenAI | Pay-as-you-go |
| `volcengine-ark-anthropic` | `https://ark.cn-beijing.volces.com/api/compatible` | Anthropic | Pay-as-you-go, Claude Code |

## Which one do I want?

- You bought a **Coding Plan / Agent Plan** subscription → use a `coding` preset.
  Your API key is the one shown on the Coding Plan page.
- You topped up **balance and pay per token** → use a `volcengine-ark` preset.
- You are wiring **Claude Code** → use an `anthropic` preset, otherwise Claude
  Code will talk OpenAI's wire format to an Anthropic endpoint.

> ⚠️ Pointing a Coding Plan key at `/api/v3` does not fail loudly. It works, and
> it bills your pay-as-you-go balance while leaving the subscription untouched.
> The reverse (`/api/v3` key on `/api/coding`) returns model-not-found errors.

## Model names are not interchangeable

Coding Plan accepts short aliases; pay-as-you-go accepts dated model IDs or an
inference endpoint ID (`ep-...`).

| Product | Examples |
|---|---|
| Coding Plan | `ark-code-latest`, `doubao-seed-code`, `doubao-seed-2.1-pro`, `glm-5.3`, `deepseek-v4.1-flash`, `kimi-k3` |
| Pay-as-you-go | `doubao-seed-2-1-pro-260915`, `ep-2024...` |

`ark-code-latest` is a console-managed alias: change the model in the Ark
console and it takes effect in 3–5 minutes without touching AI Switch.

AI Switch probes with the family that matches the base URL, so adding the
vendor and then running a health check normally fills the model list for you.
If detection fails, set the model explicitly on the key — a value copied from
the console always wins over discovery.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `model not found` / model errors only | Model name from the other product, or `ark-code-latest` on pay-as-you-go |
| Requests succeed but the pay-as-you-go bill grows | Base URL is `/api/v3` while you meant to use the Coding Plan |
| 401 `The API key format is incorrect` | Key copied with whitespace or a newline |
| 401 `The API key doesn't exist` | Key belongs to the other product, or was deleted |
| Claude Code reports a protocol error | Using an OpenAI preset instead of an `anthropic` one |

If your site uses an internal gateway in front of Ark, create a custom provider
whose `api_url` ends at the same place and let endpoint detection decide the
protocol.
