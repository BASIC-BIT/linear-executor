# Linear Webhooks Developer Documentation

**Source:** https://linear.app/developers/webhooks
**Scraped:** 2026-04-18 via Firecrawl (cached from 2026-04-17)

---

## Kernfakten für Signatur-Verifikation

### Headers (von Linear gesendet)

| Header | Inhalt |
|---|---|
| `Linear-Delivery` | UUID v4 — eindeutig pro Push |
| `Linear-Event` | Entity-Typ: `Issue`, `Comment`, `Project`, ... |
| `Linear-Signature` | **HMAC-SHA256** der raw body contents, **hex-encoded**, signiert mit Webhook-Signing-Secret |
| `Content-Type` | `application/json; charset=utf-8` |
| `User-Agent` | `Linear-Webhook` |

### Signatur-Algorithmus

```
HMAC-SHA256(secret=LINEAR_WEBHOOK_SECRET, message=raw_body) → hex-encoded
Compare case-insensitive gegen Linear-Signature Header
```

**Wichtig:** Raw body nutzen, NIE re-stringified JSON — sonst Signatur-Mismatch.

### Replay-Schutz

Payload enthält `webhookTimestamp` (UNIX ms). Empfehlung: Ablehnen wenn `abs(now - webhookTimestamp) > 60s`.

### Retry-Verhalten (Linear-Seite)

- Timeout: 5000 ms
- Bei non-200: 3 Retries mit Backoff **1 min → 1 h → 6 h**
- Nach dauerhaftem Fail: Webhook kann deaktiviert werden

### Anforderungen an den Consumer

- Öffentlich erreichbare **HTTPS** URL (kein localhost)
- Response mit `HTTP 200` innerhalb 5s
- Bei Server-Error: `500` → Linear retried später (statt 401)

## Linear-IP-Adressen (für optionales IP-Allowlisting)

- 35.231.147.226
- 35.243.134.228
- 34.140.253.14
- 34.38.87.206
- 34.134.222.122
- 35.222.25.142

## Payload-Struktur (Data-Change-Events)

```json
{
  "action": "create | update | remove",
  "actor": {"id": "...", "type": "user", "name": "...", ...},
  "type": "Issue | Comment | Project | ...",
  "data": { /* serialized entity */ },
  "url": "https://linear.app/...",
  "createdAt": "2020-01-23T12:53:18.084Z",
  "updatedFrom": { /* only on update actions, previous values */ },
  "webhookTimestamp": 1676056940508,
  "webhookId": "000042e3-...",
  "organizationId": "..."
}
```

## Unterstützte Entity-Typen für Data-Change-Events

Issues, Issue attachments, Issue comments, Issue labels, Comment reactions, Projects, Project updates, Documents, Initiatives, Initiative Updates, Cycles, Customers, Customer Requests, Users.

Zusätzlich: `Issue SLA`, `OAuthApp revoked`.

## Node.js-Referenz-Implementierung (aus Linear-Doku)

```javascript
const crypto = require("node:crypto");
const LINEAR_WEBHOOK_SECRET = process.env.LINEAR_WEBHOOK_SECRET;

function verifySignature(headerSignatureString, rawBody) {
  if (typeof headerSignatureString !== "string") return false;
  const headerSignature = Buffer.from(headerSignatureString, "hex");
  const computedSignature = crypto
    .createHmac("sha256", LINEAR_WEBHOOK_SECRET)
    .update(rawBody)
    .digest();
  return crypto.timingSafeEqual(computedSignature, headerSignature);
}
```

Key points:
- `timingSafeEqual` statt `===` → verhindert Timing-Attacks
- Signatur wird als Buffer verglichen, nicht als String
- Raw body wird via express middleware `req.rawBody` bereitgestellt

## Setup im Linear-UI

Settings → Administration → API → New webhook → URL + Label, Team, Resource-Types auswählen.
Admin-Rechte erforderlich.
Signing-Secret findet sich auf der Webhook-Detail-Seite.

## Unser POC-Flow (TES-466)

1. FastAPI-Endpoint `/webhook` auf Dev-Server, hinter Caddy (HTTPS)
2. Raw body via `await request.body()` lesen — BEVOR JSON geparst wird
3. `hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()` berechnen
4. Mit `hmac.compare_digest` gegen `linear-signature` Header vergleichen
5. Bei Match: Payload als JSON parsen + loggen, 200 zurück
6. Bei Mismatch: 401
7. Replay-Schutz (60s-Window) kann später ergänzt werden, ist nicht POC-blocker
