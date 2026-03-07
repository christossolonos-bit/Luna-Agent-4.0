# Luna Process Flow: Intent to Execution

Inspired by the Adam process flow. Luna follows a simplified path from input to response.

---

## 1. Input Receiving

All interactions start with an **inbound message** from an **Adapter**.

- **Current:** Discord (mentions, DMs). Message includes: user content, author, channel, guild.
- **Future:** Web UI, CLI, Telegram — same pipeline, different adapters.

The adapter normalizes the input (strip mentions, attachments) and passes it to the next stage.

---

## 2. Intent Classification ("What to Do")

A **Classifier** decides the user's intent so we know how to handle the message.

| Intent      | Meaning              | Next step                    |
|------------|----------------------|------------------------------|
| `chat`     | Conversation, Q&A    | Direct LLM response (Ollama) |
| `task`     | Do something (run, edit, search) | Planning → Execution   |
| `help`     | Ask how Luna works   | Static or generated help     |
| `personality` | Mood, identity, preferences | Store + optional reply |

- **Current:** Everything is treated as `chat` → Ollama reply.
- **Planned:** Use a fast model or simple rules to classify; route `task` to Planner.

---

## 3. Planning ("How to Do It") — *for task intents*

When intent is **task**, a **Planner** turns the goal into steps.

- **Goal decomposition:** High-level goal → list of steps (e.g. "read file X" → "edit section Y").
- **Dependencies:** Order steps so dependencies run first.
- **Tool assignment:** Map each step to a tool (e.g. `read_file`, `run_shell`, `edit_file`).

- **Current:** Not implemented; only chat.
- **Planned:** TaskGraph / TaskQueue; tools registered and selected by the planner.

---

## 4. Execution (Action & Orchestration)

An **Executor** runs the plan.

- **Worker loop:** Take ready tasks (dependencies satisfied), run them.
- **Tool execution:** Call tool functions, pass inputs, capture outputs.
- **Write verification:** For file edits, optional read-after-write check.
- **Events:** Emit progress (Thinking, Executing, Success, Failure) to Discord or UI.

- **Current:** No task execution.
- **Planned:** Executor consumes TaskQueue; Discord shows "Luna is thinking" / "Done" / errors.

---

## 5. Self-Repair (Reflex Loop) — *when a task fails*

On failure:

- **Diagnostic:** Analyze error and context (logs, code, last step).
- **Patch proposal:** Generate a fix (e.g. unified diff).
- **Human-in-the-loop:** Propose the fix to the user; apply only after approval (e.g. "Apply? yes/no").

- **Current:** Not implemented.
- **Planned:** PatchService + approval step in Discord (e.g. reactions or command).

---

## 6. Continuous Improvement (Review Loop)

Background process that learns from history.

- **Proactive review:** Periodically scan conversations and outcomes for improvements.
- **Behavior reinforcement:** Use positive/negative feedback to tune traits (e.g. concise, proactive).
- **Golden examples:** Store "ideal" interactions as few-shot examples for the model.

- **Current:** Not implemented.
- **Planned:** Optional review job; feedback via reactions or explicit "good/bad" commands.

---

## 7. Result Synthesis

After execution (or for chat-only), the system produces a **final user-facing response**.

- **Chat:** Ollama reply, optionally trimmed/formatted for Discord.
- **Task:** Summarize what was done, show outputs or errors, suggest next steps.

- **Current:** Chat path only; Ollama reply is the final message.

---

## Summary: Luna vs Adam

| Stage              | Adam                    | Luna (current)     | Luna (target)        |
|--------------------|-------------------------|--------------------|----------------------|
| Input              | CLI, Web, Discord, TG   | Discord            | + Web, CLI           |
| Intent             | Classifier (fast model) | All → chat         | Classifier → chat/task |
| Planning           | TaskGraph, tools        | —                  | Planner + TaskGraph  |
| Execution          | Executor, EventBus      | —                  | Executor + events    |
| Self-Repair        | PatchService + approval | —                  | Optional patch + approval |
| Improvement        | Review, golden examples| —                  | Optional review loop |
| Synthesis          | Final response          | Ollama reply       | Reply or task summary |

Luna currently implements: **1. Input (Discord)** → **2. Intent = chat** → **7. Synthesis (Ollama)**. The rest of the flow is the roadmap.
