# What’s making Luna slow

Summary of the main contributors to reply latency (so you can see where time goes). All features stay; this is for visibility and tuning.

---

## 1. **Multiple Ollama calls before a single reply**

For a normal message (no `!command`), Luna can do **up to 3 full Ollama requests** before you see a reply:

| Step | What | When |
|------|------|------|
| **NL command parse** | `_parse_natural_language_command(msg)` → one Ollama call with `_NL_COMMAND_SYSTEM` to see if the message is a command (e.g. “message Marios”) | Every non-`!` message |
| **Compaction** | If conversation history has > 20 messages, `_compact_conversation_history()` → `_summarize_conversation()` → **one Ollama call** to summarize the older part | When history is long |
| **Main reply** | `ollama_chat(msg, ...)` → **one Ollama call** to generate Luna’s reply | Always for chat |

So worst case: **3 sequential Ollama round-trips** (parse → maybe summarize → reply). Each is a full request/response; no streaming.

- **Where in code:**  
  - NL parse: `_parse_natural_language_command()` → `ollama_chat(..., _NL_COMMAND_SYSTEM)` (e.g. ~6045).  
  - Compaction: `_compact_conversation_history()` → `_summarize_conversation()` → `ollama_chat(...)` (~1378).  
  - Main: `ollama_chat(msg, LUNA_SYSTEM_PROMPT, scope, history)` in `api_chat` (~6825) and Discord handler (~7292).

---

## 2. **Single long Ollama timeout**

- Main chat uses **120s** timeout: `urlopen(req, timeout=120)` in `ollama_chat`.
- So the process can sit waiting on Ollama for up to 2 minutes before failing. Shorter timeout would fail faster; longer context or slow hardware can still make each call slow.

- **Where:** `bot.py` around line ~1432 in `ollama_chat()`.

---

## 3. **No streaming**

- Ollama is called with `"stream": False`. The whole reply is generated before anything is returned.
- So you never see partial text; perceived latency = full generation time.

- **Where:** `ollama_chat()` builds the request with `stream: False` (~1424).

---

## 4. **System prompt built every time**

- For every `ollama_chat`, `_build_system_prompt()` runs and:
  - Uses cached SOUL/TOOLS/OBJECTIVES/skills (30s TTL).
  - Loads **profile**, **memory** from disk/memory per scope.
- So we still do a non-trivial amount of work and I/O on each request even with the identity cache.

- **Where:** `_build_system_prompt()` (~1233), called from `ollama_chat()` (~1413).

---

## 5. **Heavy prompt size**

- System prompt includes: base + SOUL + TOOLS + OBJECTIVES + skills + profile + 4-layer memory + identity hint.
- Bigger prompt → more tokens → slower and more memory for the model.

- **Where:** Same `_build_system_prompt()` and `_get_effective_system_prompt()`.

---

## 6. **“Do / research” and search flows**

- For “do X” / “can you do X”:
  - `_run_do_research_and_propose()` does **2 Ollama calls**: one to summarize how to do it, one to propose the action.
- For “search X” / “google X”:
  - `_recommend_best_search_result()` does **1 Ollama call** to pick the best link.
- These add extra round-trips when those intents are triggered.

- **Where:**  
  - Do: ~2136 and ~2154 in `_run_do_research_and_propose()`.  
  - Search: ~2061 in `_recommend_best_search_result()`.

---

## 7. **User style update in background**

- After sending the reply, Luna starts `_maybe_update_user_style(scope)` in a **background thread**. That does another **Ollama call** to summarize the user’s style. It doesn’t block the reply you see, but it does add load and can make the next request slightly slower if Ollama is busy.

- **Where:** e.g. `api_chat` ~6846: `threading.Thread(target=_maybe_update_user_style, ...)`; the actual Ollama call is in `_maybe_update_user_style` ~1220.

---

## 8. **Compaction summary length**

- When we compact, we summarize “up to last 30 message contents” and ask for “2–4 short sentences”. So the summarization prompt can still be large on very long chats.

- **Where:** `_summarize_conversation()` ~1352–1376 (block of messages, then one `ollama_chat`).

---

## Quick wins (without removing features)

1. **Skip or shortcut NL parse** for obvious non-commands (e.g. very short or clearly conversational messages) to avoid the first Ollama call when not needed.  
2. **Stream the main reply** from Ollama and stream to the client so the user sees tokens as they’re generated.  
3. **Tighten timeout** (e.g. 60s) so slow/failed Ollama fails faster.  
4. **Reduce prompt size** (shorter TOOLS summary, or trim old memory) to speed up inference.  
5. **Optional: run NL parse and “is long history?” in parallel** with building the rest of the context, then do a single “reply” call (would require a bit of refactor).

This file is for reference only; it isn’t loaded by the bot.
