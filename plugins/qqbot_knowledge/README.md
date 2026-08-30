# QQBot Source Knowledge

`mlj.qqbot-knowledge` is a MaiBot SDK 2.7 plugin that retrieves bounded evidence from explicitly configured local source trees. It does not answer messages itself. For a matching replyer request, it deep-copies serialized model messages and appends one temporary `<mlj.qqbot-knowledge:v1>` block to the system message.

## Data flow

1. `chat.receive.before_process` records `session_id -> group_id` only when both values exist in the structured message and `message_info.group_info`.
2. `maisaka.replyer.before_model_request` reads only the last serialized `user` message, capped by `search.max_query_chars`.
3. Explicit domain, mod, and tool names select domains before the optional group bias. The exact tools `SaveDataExporter`, `UXAEnhance`, `AfterBuildEvent`, `GetDspData`, and `VanillaCurveSim` select `dsp-mod-tools` from any group.
4. The standard-library index keeps a bounded candidate set for every configured root, then query-sorts the combined domain candidates and reads at most `max_files_per_domain` files. Search remains within result, character, time, and cache budgets.
5. One evidence block is added to a deep copy. The block explicitly requires evidence-only factual claims, QQ plain text, no Markdown, and an uncertainty statement when the excerpts do not support a field, number, or behavior. Existing blocks are detected, so retries never append a second block. All hook kwargs are preserved for Core.

No Embedding, `ctx.knowledge`, provider, fallback, extra LLM, external API, or plugin dependency is used.

## Security boundary

Source roots come only from structured `[[sources.roots]]` entries. Roots must be absolute, existing, non-symlink paths. Every candidate is resolved and proven to remain inside its configured root before being read. Symlinks, ignored build/cache directories, sensitive path components, unsupported extensions, binary files, and oversized files are rejected. Reads request at most `max_file_bytes + 1` bytes. Prompt references contain only the configured domain and a root-relative path, never an absolute path.

Logs contain domains, result count, evidence character count, elapsed time, and normalized error type. They do not contain queries, prompts, excerpts, source roots, absolute paths, or credentials.

## Scope and budgets

The default `scope.enabled_group_ids = []` does not gate requests. When the list is non-empty, a request is eligible only if an unexpired structured receive event established its exact group; missing facts fail closed. Session IDs are opaque keys and are never parsed.

Defaults:

- 4 results and 2600 evidence characters
- 80 candidate files per configured root and at most 80 files read per queried domain
- 220000 bytes per file
- 2000 query characters
- 3 seconds per search
- 600-second source refresh, result cache, and session scope TTL

Source and decompiled source, then adjacent README/design material, are preferred. CHANGELOG and release-note files are supplemental and sort after primary evidence. The default Shapez domain uses `DecompiledSource/Game.Content`, `shapez-mods/src`, and `shapezPathAnalyzer/shapezAnalyzer`; it does not rely on the removed `shapez.io/src/js` tree.

Copy `config.example.toml` to the plugin configuration location and adjust only the structured source roots that exist on the MaiBot host. Configuration reload reconstructs the index and clears both result and session caches.
