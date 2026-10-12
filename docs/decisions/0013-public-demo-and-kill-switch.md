# 0013: A public demo that can't break or bankrupt the gateway

**Status:** accepted (M4). Code: `gateway/api/demo.py`, `gateway/routing/dispatch.py`,
`gateway/routing/breaker.py`, `gateway/api/admin.py`, `web/`.

## The tension
The demo should let anyone:
1. send real requests,
2. see failover happen, and
3. see the dashboard react.

Done naively, (1) is an open wallet, and (2) is a button that lets one visitor take a provider
down for everyone.

## Decisions

### Demo keys: tiny, scoped, and rate-limited at creation
`POST /demo/keys` issues a normal API key with:
- a **$0.02 monthly spend cap**
- **10 requests/min** and **20k tokens/min**
- **cheap models only**, plus `auto` (and the strong model behind it, without which `auto`
  could never route "strong")

The caps reuse the existing limiters (reserve-then-settle and token buckets), so they hold
across replicas exactly like real keys. Minting keys is itself limited to **30 per hour,
globally**, using a Redis counter shared by all replicas. That bounds the worst case at
**30 × $0.02 = $0.60 per hour** no matter how many visitors there are.

Demo keys are off by default (`DEMO_ENABLED=false`). Compose turns them on for local use.

### Failover demo: per-request simulation, not a global switch
The playground's "simulate an outage" sends `X-Gateway-Simulate-Outage: groq`. The dispatcher
then skips that provider **for this request only**: it records no breaker failure and touches
no shared state. The visitor sees a real fallback (`X-Gateway-Fallbacks: 1`, a different
provider) without affecting anyone else's traffic.

**Rejected alternative:** let the playground flip the real kill switch. It's more dramatic, but
one visitor could then degrade the service for everyone, which is unacceptable on a public URL.

### Operator kill switch: admin-only and shared
The dashboard's **"Take down"** sets a forced-down flag in Redis. The breaker's admission Lua
script checks the flag first, so **every replica** stops sending to that provider and traffic
fails over along the fallback chains, exactly as in a real outage. That's the point of the
operator view.

The flag needs the admin token, and it propagates within the 1 s local "closed" cache
(ADR 0006). It's reflected immediately on the replica that handled the click.

### UI served from the same origin
nginx serves the built React app and proxies `/v1`, `/admin` and `/demo` to the gateway
replicas, so there's no CORS to configure, and the admin token never needs to cross origins.
The UI keeps:
- the admin token in `sessionStorage` (gone when the tab closes)
- the API key in `localStorage`, for convenience in the playground.

### No UI dependencies beyond React
Charts are ~150 lines of hand-written SVG, and Markdown rendering is a small safe renderer
that builds React elements and never sets innerHTML. Both stay within the project's dependency
rules. The production bundle is about 54 KB gzipped.
