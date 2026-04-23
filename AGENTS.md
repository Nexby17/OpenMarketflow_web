# AGENTS.md — HedgeFund Project

Drop-in operating instructions. Read before every task.

**Working code only. Finish the job. Plausibility is not correctness.**

---

## 0. Non-negotiables

1. **No flattery, no filler.** Skip "Great question!", "You're right!", "I'd be happy to". Start with the action.
2. **Disagree when you disagree.** If the premise is wrong, say so before doing the work.
3. **Never fabricate.** Not file paths, API names, test results. If unsure — read the file, run the command, or say "I don't know, let me check."
4. **Stop when confused.** Two plausible interpretations → ask. Do not pick silently.
5. **Touch only what you must.** Every changed line traces to the user's request. No drive-by refactors.
6. **Single connector, single data source.** If QUIK is active — ONLY QUIK data. If Finam — ONLY Finam. No fallback mixing. No "salad of data from different connectors."

---

## 1. Before writing code

- State the plan in 1-2 sentences before editing. For non-trivial tasks — numbered steps with verification for each.
- Read the files you will touch. Read the files that CALL the files you will touch.
- Match existing patterns in the codebase. Do not introduce new patterns without reason.
- Surface assumptions: "I'm assuming X, Y, Z. If wrong — say so."
- If two approaches exist — present both with tradeoffs. Do not pick silently.

---

## 2. Simplicity first

- No features beyond what was asked.
- No abstractions for single-use code.
- If the solution runs 200 lines and could be 50 — rewrite before showing.
- Bias toward deleting code over adding code.

---

## 3. Surgical changes

- Do not "improve" adjacent code, comments, formatting not part of the task.
- Do not refactor working code just because you're in the file.
- Do clean up orphans YOUR OWN changes created (unused imports, variables).
- Match existing style exactly: indentation, quotes, naming, file layout.

---

## 4. Goal-driven execution

Rewrite vague asks into verifiable goals:

- "Fix the bug" → reproduce, fix, verify.
- "Make it work" → define "works" as testable criteria, then verify.
- For UI changes: verify with puppeteer/screenshot before claiming done.

For every task:
1. State success criteria before writing code.
2. Write verification.
3. Run it. Read output. Do not claim success without checking.
4. If verification fails — fix cause, not the test.

---

## 5. Project context

### Stack
- **Backend:** C# / .NET 8 / ASP.NET Minimal API (top-level Program.cs)
- **Frontend:** Vanilla JS + Lightweight Charts + SignalR
- **Data:** Python (numba/numpy) for backtesting
- **Broker:** Finam Trade API (gRPC + REST)
- **Terminal data:** QUIK via Lua bridge → HTTP POST
- **Server:** 216.57.106.32:5050

### Commands
- **Build:** `cd src && dotnet publish Server/HedgeFund.Server.csproj -c Release -o Server/bin/Release/net8.0/publish/`
- **Static files:** `cp Server/wwwroot/js/app.js Server/bin/Release/net8.0/publish/wwwroot/js/app.js` (AFTER publish — dotnet overwrites wwwroot)
- **Restart:** `kill -9 $(lsof -ti:5050); sleep 5; cd Server/bin/Release/net8.0/publish && ./HedgeFund.Server &`
- **Kill must use kill -9 with PID from lsof -ti:5050** — fuser, pkill unreliable

### Layout
- **Server:** `src/Server/` — Program.cs, Services/, Hubs/, wwwroot/
- **Strategy:** `src/Core/Strategies/GridMmRegimeStrategy.cs`
- **Launcher:** `src/Server/Services/GridMmRegimeLauncher.cs`
- **Frontend:** `src/Server/wwwroot/js/app.js`, `src/Server/wwwroot/index.html`
- **Backtest:** `backtest/src/` — Python scripts
- **Data:** `backtest/data/` — CSV files

### Architecture — Connectors
- **One active connector** at a time, user-selectable via UI dropdown
- `IConnector` interface → `ConnectorManager` holds active connector
- Adapters: `FinamConnectorAdapter`, `QuikConnectorAdapter`, `TransaqConnectorAdapter`
- `_activeConnectorName` in Program.cs — "Finam", "QUIK", or "Transaq"
- **ALL data flows through active connector. No fallback to other connectors.**

### Conventions
- **C# top-level Program.cs:** No record/class declarations after top-level statements. Use JsonDocument parsing.
- **C# top-level locals:** No `volatile` or `readonly` modifiers.
- **JS safety:** Optional chaining `?.` ONLY on reads (right side). NEVER on assignments.
- **EMA=30** — the ONLY correct value. Do NOT change without explicit approval.
- **Grid direction:** LONG → SELL limits ABOVE (closing). SHORT → BUY limits BELOW (closing). NEVER invert.
- **Version bump:** After every JS change → bump `app.js?v=N` in index.html. User may need Ctrl+Shift+R.

### Forbidden
- Do NOT mix data from QUIK and Finam in the same UI. One connector = one data source.
- Do NOT add fallback from one connector to another. If active connector has no data → show "no data", not data from another connector.
- Do NOT touch Program.cs type system (no record/class after top-level statements).
- Do NOT spawn subagents for Program.cs edits — they break top-level structure.

---

## 6. Session hygiene

- After 2 failed corrections on the same issue — stop. Summarize what you learned and ask for a sharper prompt.
- Context is the constraint. Fresh session > bloated session.
- Commit messages: descriptive, under 72 chars. No "update file" or "fix bug".

---

## 7. Communication

- Direct. "This won't work because X" > "Interesting approach, but..."
- Concise. 2-3 short paragraphs unless depth requested.
- When a question has a clear answer — give it. When not — say so and give tradeoffs.

---

## 8. When to ask vs proceed

**Ask before proceeding when:**
- Two plausible interpretations, choice materially affects output.
- Change touches load-bearing/versioned code.
- Need credentials/secrets/production access.
- User's stated goal and literal request conflict.

**Proceed without asking when:**
- Trivial and reversible (typo, rename, log line).
- Ambiguity resolved by reading code or running command.

---

## 9. Self-improvement loop

After every session where something went wrong:
1. Was the mistake a missing rule or an ignored rule?
2. If missing → add concrete rule to Section 10 (Project Learnings).
3. If ignored → tighten or move up the existing rule.
4. Prune regularly. Under 300 lines. Over 500 = fighting your own config.

---

## 10. Project Learnings

- Always verify QUIK JSON field names match code (QUIK uses `"t"` for ticker, not `"s"`)
- `updateLivePrice` must validate that quote price is in reasonable range before updating chart (5% check prevents chart corruption from wrong-ticker data)
- `candleBuilderHistory` is empty when gRPC is not streaming — do not rely on it for quote fallback
- Finam REST candles URL gives 301 redirect — old URL `/v1/instruments/{symbol}/candles` does not work
- SignalR `SendAsync("OnQuoteUpdate", quotes.ToString())` sends a JSON STRING not an object — JS receives string, all fields undefined, becomes 0.00
- **HARD REVERT to old git commit does NOT guarantee everything works** — data/state may differ
- **One change at a time. Verify. Then next change.** Multiple simultaneous changes = guaranteed breakage.
- **Ask user what "doesn't work" means specifically** before touching code. "Doesn't work" is not actionable.
- `api.finam.ru` REST needs FINAM_API_KEY (not JWT) via /v1/sessions. Symbol format: SiM6@RTSX (futures), SBER@MISX (stocks)
- Finam REST JWT expires 15 min — refresh with 1 min margin
- .env at src/.env loaded at startup for FINAM_API_KEY + FINAM_ACCOUNT_ID
- `api.finam.ru/v1/accounts/` needs trailing slash or returns 308 redirect to website
- **Always ask for missing credentials BEFORE coding**, not after
- CDN links (unpkg, cdnjs) cause 30s load times from VPS — always use local JS files
- `/api/quotes` batch is faster than 6 individual `/api/quote` calls (500ms vs 600ms+ sequential)

---

## 11. Current priorities

1. Connector-based data isolation (QUIK-only or Finam-only, no mixing)
2. Trading tab: show real trades/orders from active connector
3. Fix Finam REST API for non-SiM6 instruments (orderbook endpoint)
