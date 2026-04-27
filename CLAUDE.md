# linear-executor

FastAPI-Bridge zwischen Linear und Claude Code. Linear-Ticket wechselt auf Status "Do It" → Webhook → Endpoint startet `claude --print` im passenden Folder → Ergebnis zurück als Linear-Kommentar.

## Linear-Tickets

- **TES-466** (POC, Urgent) — Webhook empfangen + loggen
- **TES-458** — Executor-Architektur (Filter "Do It", async Job, Kommentar zurück)
- **TES-464** — Status "Do It" als Trigger + MCP-Tool `set_linear_status`
- **TES-465** — Ad-hoc Proxy (`type: proxy` Tickets für Mobile)
- **TES-457** — Vision (Linear als AI-Steuerungssystem)

## Phasen

| Phase | Umfang | Ticket |
|-------|--------|--------|
| 1 | POC: Webhook empfangen, HMAC-Signatur prüfen, Payload loggen | TES-466 |
| 2 | Executor: Filter "Do It", `cc_folder` extrahieren, `claude --print` async starten, Kommentar + Status zurück | TES-458 |
| 3 | MCP-Tool `set_linear_status` → Trigger aus Claude-Chat | TES-464 |
| 4 | Ad-hoc-Proxy: `type: proxy` Tickets, generic Folder, Mobile-Flow | TES-465 |
| 5 | Cron-Feature (später, evtl. via `claudeclaw:jobs` statt eigenes System) | TES-458 (Teil) |

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

## Struktur (wird aufgebaut bei Implementierung)

Noch kein Code — erst wenn TES-466 in Progress geht.
