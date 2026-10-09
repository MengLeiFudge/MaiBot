# QQBot Knowledge

`mlj.qqbot-knowledge` is a MaiBot SDK 2.7 plugin that injects bounded source evidence into one Replyer request. It does not answer messages itself.

## Shared DSP retrieval

DSP knowledge has one physical owner: Yunqi's AstrBot `dsp-major-mods` vector knowledge base. For every Replyer request, this plugin sends the bounded current query and structured group ID to `http://127.0.0.1:8081/v1/knowledge/dsp/search`.

The AstrBot service decides whether the query belongs to DSP. Matching requests use Qwen embedding recall, AstrBot's dense/sparse fusion, and `qwen3-rerank`. Returned evidence is injected into Yelin's existing model request, so Yelin's own chat model still writes the final response. Yelin does not store, update, or embed a second DSP corpus.

The shared corpus covers the authoritative source and structured gameplay data for:

- Fractionate Everything
- Project Genesis
- Project Orbital Ring
- More Mega Structures
- They Come From Void / DSP Battle
- Project Eden

If the shared service is synchronizing or unavailable, the hook continues without DSP evidence and logs only a normalized error type.

## Preserved local domains

Shapez and Factorio retain the existing bounded standard-library source search. This fallback runs only when the shared service says the query is not DSP, or when the shared service is unreachable and one of the configured local domains matches explicitly.

## Request flow

1. `chat.receive.before_process` records `session_id -> group_id` only from structured group messages.
2. `maisaka.replyer.before_model_request` reads the last `UserMessageItem.parts` in Context Item schema 1, capped by `search.max_query_chars`.
3. The loopback DSP service is queried first. A non-DSP result may then select Shapez or Factorio local roots.
4. One `<mlj.qqbot-knowledge:v1>` evidence block is added as a `SystemMessageItem`. Existing Items and their IDs, relationships, and content stay unchanged. Repeated injection recognizes the plugin's stable evidence Item ID.
5. The block requires evidence-only factual claims, plain QQ text, and an explicit uncertainty statement when the excerpts do not establish the requested fact.

## Security boundary

The DSP endpoint must be an unauthenticated HTTP URL on `127.0.0.1`, `localhost`, or `::1`; other hosts and embedded credentials are rejected during plugin load. Query and response sizes are bounded. Logs never contain queries, prompts, excerpts, source roots, absolute paths, or credentials.

Local Shapez and Factorio roots retain their prior path-containment, symlink, extension, binary, size, timeout, and cache restrictions. Copy `config.example.toml` to the plugin configuration location when creating a new runtime configuration.
