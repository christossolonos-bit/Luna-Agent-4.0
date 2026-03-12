# Luna Boss + Employees — Who Does What, Who Uses Whom

Luna is the **Boss**. She receives every message, routes it, and either delegates to a small-task **employee** or does the main reply herself.

---

## Roster (all implemented)

| Name | Role | What they do | Model | Used by |
|------|------|--------------|--------|---------|
| **Commander** | Employee | Parses natural language into command + params. | `OLLAMA_MODEL_SMALL` | Boss in `api_chat` and `on_message` when message looks like a command. |
| **Scribe** | Employee | Summarizes long conversation into 2–4 sentences (compaction). | `OLLAMA_MODEL_SMALL` | Boss inside `_compact_conversation_history`. |
| **SearchPicker** | Employee | Picks best search result + short reason from results list. | `OLLAMA_MODEL_SMALL` | Boss in `_open_google_search`. |
| **Copywriter** | Employee | Short WhatsApp message from context (when user gives description for !msg). | `OLLAMA_MODEL` | Boss when generating message for WhatsApp. |
| **Lyricist** | Employee | Song lyrics from description (local music). In `local_music.generate_lyrics`. | Ollama (in local_music) | `create_local_song_project`. |
| **Receptionist** | Employee | Returns "what can you do" / commands help (no Ollama). | — | Boss when user asks for help. |
| **Newsroom** | Employee | Fetches and formats world news (no Ollama). | — | Boss when user says news. |
| **Luna** | Boss | Main chat reply. Streams on web. | `OLLAMA_MODEL` | Boss in `api_chat` and `on_message`. |

---

## Flow (who uses what)

1. **User sends a message** (web or Discord).
2. **Boss (Luna)** receives it. Pending/confirmations (file save, run script, etc.) are handled first.
3. **Boss** checks: does the message look like a command?  
   - If **yes** → **Commander** parses it. If a command is returned, Boss runs it and replies; done.  
   - If **no** → skip Commander.
4. **Boss** loads conversation history. If it's long → **Scribe** summarizes the older part.
5. **Boss (Luna)** does the main reply (streaming on web, sync on Discord). Post-process, then respond.

Other flows: **SearchPicker** for "search X"; **Copywriter** for WhatsApp message from context; **Lyricist** inside local song creation; **Receptionist** for help; **Newsroom** for news.

---

## In code (`bot.py`)

- **Constants:** `EMPLOYEE_COMMANDER`, `EMPLOYEE_SCRIBE`, `EMPLOYEE_LUNA`, `EMPLOYEE_SEARCH_PICKER`, `EMPLOYEE_COPYWRITER`, `EMPLOYEE_LYRICIST`, `EMPLOYEE_RECEPTIONIST`, `EMPLOYEE_NEWSROOM`.
- **Wrappers:** `employee_commander`, `employee_scribe`, `employee_search_picker`, `employee_copywriter_whatsapp`, `employee_lyricist`, `employee_receptionist`, `employee_newsroom`.

This file is for reference only; the bot does not load it.
