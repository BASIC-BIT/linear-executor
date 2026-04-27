# EU Vector-DB Empfehlung fuer GDPR-konforme RAG

**Empfehlung: Qdrant (self-hosted in EU-Region)** als primaere Wahl.

Kurzbegruendung (basierend auf TES-614/630):

- **Qdrant** ist Open-Source (Apache-2.0), laesst sich auf EU-VPS (Hostinger/Hetzner/Scaleway) self-hosten — volle Datenhoheit, kein US-Cloud-Act-Exposure.
- Performance- und Filter-Features (Payload-Filter, hybride Suche) decken die typischen RAG-Workloads ab; Rust-Core gibt vorhersehbare Latenzen.
- **Fallback:** Weaviate (ebenfalls self-hostable, EU-Cloud-Region verfuegbar) wenn Multi-Tenancy / GraphQL-Native gebraucht wird.
- **Nicht empfohlen** fuer GDPR-strikt: Pinecone (US-only, kein EU-Self-Host), Chroma (Single-Node, kein Production-EU-Story).
