# linear-executor

FastAPI-Bridge zwischen Linear und beliebigen AI-Coding-Agents. Linear-Ticket wechselt auf Status **"AI Implementation"** → Webhook → Executor pickt den CLI per Linear-Label `cli:<name>` (claude/opencode/codex/gemini/forge), startet ihn headless im passenden Folder (Worktree) → Ergebnis zurück als Linear-Kommentar.

Status-Konstanten (siehe `app/filter.py`):
- Trigger: `AI Implementation`
- Review: `In Review`
- Cancel: `Stop AI` (sendet SIGTERM an den Subprocess)
- Batch: `AI Batch`
- Complete: `Done`

CLI-Auswahl per Label `cli:<name>`, Default `claude` wenn kein Label gesetzt (`app/cli_registry.py`).

## Linear-Tickets (Frühphase + Meilensteine)

- **TES-466** (POC, Urgent) — Webhook empfangen + loggen
- **TES-458** — Executor-Architektur (Filter, async Job, Kommentar zurück)
- **TES-464** — MCP-Tool `set_linear_status` als Trigger aus dem Chat
- **TES-465** — Ad-hoc Proxy (`type: proxy` Tickets für Mobile)
- **TES-457** — Vision (Linear als AI-Steuerungssystem)
- **TES-646** — CLI-agnostisch via `cli:<name>` Labels (multi-engine support)

Aktuelle Tickets im Code/README referenziert: TES-596, TES-606, TES-608, TES-615, TES-617, TES-619, TES-700, TES-702 — siehe README + Codebase.

## Phasen

| Phase | Umfang | Ticket |
|-------|--------|--------|
| 1 | POC: Webhook empfangen, HMAC-Signatur prüfen, Payload loggen | TES-466 |
| 2 | Executor: Filter `AI Implementation`, `cc_folder` extrahieren, CLI async starten, Kommentar + Status zurück | TES-458 |
| 3 | MCP-Tool `set_linear_status` → Trigger aus Claude-Chat | TES-464 |
| 4 | Ad-hoc-Proxy: `type: proxy` Tickets, generic Folder, Mobile-Flow | TES-465 |
| 5 | Cron-Feature (später, evtl. via `claudeclaw:jobs` statt eigenes System) | TES-458 (Teil) |
| 9 | CLI-agnostisch (claude/opencode/codex/gemini/forge per Label) | TES-646 |

## Deployment

- **Jetzt (Phase 1–4):** Dev-Server (`ai-devhub-247.site`), native oder Docker, hinter Caddy
- **Langfristig:** App-Server — mehr Power, dedizierter User für Cron
  - Auf App-Server läuft kein ClaudeClaw, Executor läuft unter eigenem User mit User-Cron
- Subdomain-Vorschlag: `executor.ai-devhub-247.site` (dev) → später App-Server

## Security (Phase 1)

Nur eins: **HMAC-Signatur prüfen.** Linear schickt `linear-signature` Header (SHA-256 über Payload + Secret). Wenn Signatur passt → echter Linear-Webhook. Wenn nicht → 401. ~15 Zeilen Code.

Alles andere (Auth für `/ask`, Rate-Limits, Subprocess-Governance, Cron-Rechte) kommt in späteren Phasen.

## Offene Fragen

1. Queue mit max. 1 gleichzeitig (sequentiell) vs. parallele Claude-Code-Prozesse?
2. Executor unter welchem User auf App-Server? (neuer System-User, z.B. `linear-exec`)
3. Cron: eigenes System in Phase 5 oder via `claudeclaw:jobs` / `schedule` Skill integrieren?

## Struktur

Code ist implementiert und läuft auf dem Dev-Server (`executor.ai-devhub-247.site`). Repo öffentlich auf `miraculix95/linear-executor`. Aktueller Stand:

```
app/
  main.py                         FastAPI app + Webhook-Entrypoint
  signature.py                    HMAC-Verifikation
  filter.py                       Status-Konstanten + Trigger-Matching
  cli_registry.py                 cli:<name> + model:<id> Label-Resolver
  runner.py                       Subprocess-Spawn + Timeout
  orchestrator.py                 Lifecycle: claim → run → kommentieren
  worker.py                       Polling-Worker
  queue.py                        SQLite-Queue mit status_comment_id
  cancel.py                       "Stop AI" → SIGTERM
  linear_api.py                   Linear-REST + GraphQL-Helpers
  folders.py                      cc_folder Mapping pro Ticket
  git_ops.py                      Worktree-Handling
  attachments.py                  File-Uploads an Linear-Tickets
  job_registry.py                 Job-Tracking
```

Komplette README mit Architektur-Bild, Configuration, Quick-Start: `README.md`.
