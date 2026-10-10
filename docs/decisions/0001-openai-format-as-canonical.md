# 0001: The OpenAI Chat Completions format is the internal format

**Status:** accepted (M1)

## Context
Clients talk to the gateway in OpenAI format. Upstreams speak four dialects: OpenAI-native (OpenAI,
Groq, Ollama), Anthropic Messages, and Gemini `generateContent`. Something has to sit in the middle.

## Decision
Use the OpenAI request, response and chunk shapes (`gateway/core/schemas.py`) as the only internal
representation. Every adapter translates native ⇄ OpenAI at its own boundary, and everything else
(logging, cost, caching, routing) only ever sees OpenAI shapes.

Pydantic models use `extra="allow"`, so OpenAI fields we don't model (`logprobs`, `seed`, ...) pass
through unchanged to OpenAI-compatible upstreams.

## Alternatives
- **A neutral internal format of our own.** It's cleaner on paper, but it means two translations
  per request even for the common OpenAI-compatible case, plus a format nobody else knows.
- **Pass raw JSON through and only peek at a few fields.** That's fast to build, but there's no
  validation, and each feature (cost, cache keys, routing) would re-parse provider-specific payloads.

## Consequences
- OpenAI-compatible adapters are about 50 lines in total. The Anthropic and Gemini adapters carry
  all the complexity in one place, and they're unit-tested on both directions plus streaming.
- Features that don't map get a `BadRequest` from the adapter instead of being silently dropped:
  `n>1` and JSON mode on Anthropic, remote image URLs on Gemini. M3's hard-rule routing uses the
  `supports_*` flags in `models.yaml` so these requests never reach a model that can't serve them.
- Lossy spots, documented in the adapters:
  - Anthropic temperature is clamped to 0–1.
  - Anthropic gets either `temperature` or `top_p`, never both.
  - Gemini thinking tokens are billed as completion tokens.
  - Anthropic cache-read and cache-write tokens count as prompt tokens.
