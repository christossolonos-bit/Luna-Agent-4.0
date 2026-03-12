# Features removed or declined (clunkiness) + JARVIS/OpenClaw not yet added

Use this list to **choose what to bring back**. With Luna = chat/memory and **Shadow = commands**, heavy features can live on the Shadow path so Luna stays light.

---

## A) Removed for clunkiness (can re-add as Shadow-only)

| # | Feature | What it was | Why removed | Re-add as |
|---|---------|-------------|-------------|-----------|
| 1 | **Action log (audit trail)** | Log file creates, shares, runs, etc. in `data/` or Luna projects. "What did Luna do?" | Extra I/O, code noise | Optional: Shadow logs its own runs to `data/action_log.jsonl` (no Luna prompt change). |
| 2 | **Small agents** | "Create an agent that does X" → Architect + Coder + Reviewer (Ollama) → generate Python script, save, open in Cursor. | Multiple Ollama calls, complex flow | **Shadow, create agent that …** — agent-creation flow runs only when you ask Shadow; Luna unchanged. |
| 3 | **Research / "do" flow** | "Do X" → research how, propose action, user says yes → execute (e.g. file write in Luna projects). | 2+ Ollama calls, confirm state | **Shadow, research how to …** or **Shadow, do …** — research + propose + execute only on Shadow path. |
| 4 | **Style adaptation** | Learn user's reply length, tone, TTS prefs from recent messages; feed into system prompt. | Background Ollama call after every reply | Optional: lighter version (e.g. store 1–2 prefs in profile, no Ollama). Or **Shadow, set my style …** to avoid Luna prompt bloat. |
| 5 | **Mistake explanation (LLM)** | "Why did you make a mistake?" → Ollama explains the failure. | Extra Ollama for little gain | Currently: raw error only. Could re-add as **Shadow, explain last error** (one Ollama call only when asked). |

---

## B) JARVIS plan — not yet implemented (or only partly)

| # | Feature | Source | What it would do | Notes |
|---|---------|--------|------------------|--------|
| 6 | **Goal-aware memory** | JARVIS Tier 2 | Tag some memories as "goals" (e.g. "finish the Discord game"); Luna references them in chat. | Needs memory schema + UI or phrase ("remember my goal: …"). |
| 7 | **Anticipate intent** | JARVIS Tier 2 | Use profile + recent memory to suggest next steps or "want me to create/save that?" | Proactive; could be light (no extra Ollama if rule-based). |
| 8 | **Proactive nudges** | JARVIS Tier 3 | "You might want to …" or "Last time you were …" at session start. | Depends on goal-aware memory and/or scheduling. |
| 9 | **Richer "what Luna can do" UI** | JARVIS Tier 3 | Web/Discord view of permissions and command list (TOOLS as UI). | Read-only page or modal; no new backend logic. |

---

## C) OpenClaw-style — mentioned, not implemented

| # | Feature | What it would do | Notes |
|---|---------|------------------|--------|
| 10 | **!status / !new / !think / !usage** | **!status** — short system status; **!new** — reset working/short-term memory; **!think** — toggle verbosity; **!usage** — token or request stats. | Chat controls; can be added as explicit commands (no NL parse). |
| 11 | **Model failover** | If primary Ollama model fails or is slow, fall back to another model. | Config + one retry path in `ollama_chat`. |
| 12 | **Cron / webhook** | Scheduled tasks (e.g. "every day 9am run Shadow, news") or HTTP webhook to trigger a command. | Scheduler already exists for X/Facebook; extend to generic "run Shadow command at time" or webhook. |
| 13 | **Wake word + talk mode** | Voice: wake word then talk (we have Shadow as text wake word; "talk mode" = continuous voice input). | Partially there (Shadow); full voice would need mic + STT + mode. |

---

## Summary: pick what to bring back

- **A1–A5** — Previously removed; re-adding as **Shadow-only** (or optional lightweight) keeps Luna light.
- **B6–B9** — JARVIS ideas; add when you want more "assistant" behavior or UI.
- **C10–C13** — OpenClaw-style; small commands (C10, C11) vs bigger (C12, C13).

Tell me which numbers (e.g. "2, 3, 10") you want implemented next and I’ll wire them.
