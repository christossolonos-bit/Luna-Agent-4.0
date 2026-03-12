# Luna as Boss + Employees (Hierarchy)

## Is it slow because it’s one big bot?

**Yes, partly.** One process and one “brain” (the same big model) are doing everything:

- **One big prompt** – SOUL + TOOLS + OBJECTIVES + skills + profile + memory. Every request pays the cost of that size.
- **One model for everything** – The same LLM does: “Is this a command?”, “Summarize this convo”, “Generate a reply”, “Parse this into JSON”. The first two don’t need the full Luna personality; they’re small tasks.
- **Sequential pipeline** – Parse → maybe summarize → reply. No real delegation; it’s one chain.
- **No specialization** – Command parsing would be fine with a tiny, fast model. We still use the big one (or skip with heuristics).

So “one big bot” means: one heavy model, one huge context, and several sequential steps before you see a reply.

---

## How would a hierarchy help?

**Luna = boss.** She only decides: “What kind of request is this?” and delegates.

**Employees = small, focused workers.** Each has a single job and a small prompt.

| Role | Job | Model / cost | Benefit |
|------|-----|--------------|---------|
| **Router** | “Command vs chat vs other?” | Tiny model or rules | One very fast step; only command-like messages get parsed. |
| **Command parser** | “Turn ‘message Marios hey’ into `{command: msg, contact: Marios, description: hey}`” | Small/fast model | No SOUL/TOOLS/memory; just a short system prompt. Fast. |
| **Summarizer** | “Compress this conversation into 2–4 sentences.” | Small/fast model | Same as now but with a small model → cheap compaction. |
| **Chat (Luna)** | “Reply as Luna with full context.” | Big model (e.g. deepseek-r1) | Only this step gets the full prompt and streaming. |

**Why it’s better:**

1. **Cheap steps stay cheap** – Parse and summarize use a small model → low latency and less load.
2. **Heavy step is only when needed** – The big model runs once per turn, for the actual reply.
3. **Clear separation** – Easier to tune: improve the router without touching chat; improve chat without touching parse.
4. **Possible parallelism** – e.g. “Load history” and “Run router” could overlap (with some refactor).

You don’t need separate processes or services to start. You can implement “boss + employees” inside the same app: different functions, different `OLLAMA_MODEL` (or a new `OLLAMA_MODEL_SMALL`), same process.

---

## What we can do next (without full rewrite)

### 1. **Small model for “employees” (quick win)** ✅ Implemented

- Set **`OLLAMA_MODEL_SMALL`** in `.env` (e.g. `OLLAMA_MODEL_SMALL=phi3:mini`). If unset, the main model is used.
- Used for:
  - **NL command parse** – `_parse_natural_language_command()` uses the small model.
  - **Summarization** – `_summarize_conversation()` uses the small model.
- Main model (e.g. deepseek-r1) is used only for the **main chat** reply (and streaming).
- Effect: The two “helper” calls become much faster when a small model is configured; the only heavy call is the main reply.

### 2. **Router before parse (already partly done)**

- We already have `_message_likely_command()`: skip NL parse when the message doesn’t look like a command.
- Optional: add a **tiny router** (rules or a very small model) that only outputs “command” / “chat”. Use it only when heuristics are unsure. That way we rarely run the full command parser on plain chat.

### 3. **Hierarchy in code (refactor over time)**

- **Router** – `luna_router(message) → "chat" | "command" | "news" | ...`
- **Workers** – `command_worker(message)`, `summarize_worker(messages)`, `chat_worker(message, context)`
- **Boss** – `handle(message)`: router → if command → command_worker → run; if chat → maybe summarize_worker → chat_worker → stream.
- Same process, same Ollama; just clearer roles and the option to assign different models to each worker.

### 4. **Split processes (later, if needed)**

- Run Discord and Web in separate processes (e.g. one runs Discord + Flask, another runs “workers” that call Ollama). Only if you hit limits of one process or want to scale.

---

## Summary

- **Yes** – having one big bot (one model, one huge prompt, sequential steps) is a big reason it can feel slow.
- **A hierarchy helps** – Luna as boss (route) + employees (parse, summarize, then chat with the big model) makes small tasks cheap and keeps the heavy call for the actual reply.
- **Next step you can do now** – Use a **small Ollama model** for NL parse and summarization; keep the big model only for main chat. No new services, just a second model and a bit of config.

This file is for design only; the bot doesn’t load it.
