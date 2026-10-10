# 0002: API keys are SHA-256 hashed, with a 60s in-process lookup cache

**Status:** accepted (M1). The Redis layer arrives in M2.

## Decision
- A key is `gw-` plus 32 random bytes (urlsafe base64). Only `sha256(key)` is stored, plus a 7-char
  display prefix. The raw key is shown once, at creation.
- Each replica keeps an in-process TTL cache (60s, max 10k entries, LRU) in front of Postgres.
  Only hits are cached.

## Why plain SHA-256 and not bcrypt/argon2
Slow hashes exist to protect *low-entropy* secrets (passwords) against offline dictionary attacks.
Our keys have 256 bits of entropy, so brute-forcing a leaked hash is infeasible even with a fast
hash. We also need to hash on every request to look the key up, and a deliberately slow hash
(around 100ms) would dominate gateway overhead. GitHub and Stripe-style API tokens use the same
reasoning.

## Why a cache, and why 60s of staleness is acceptable
Without a cache, every request costs a Postgres round trip before we even talk to a provider.
The cost is that **a revoked key keeps working for up to 60s on each replica that has it cached.**
That's fine for this system: revocation is rare and manual, and spend caps bound the damage, since
they're enforced from Redis on every request regardless of this cache (M2).
If instant revocation becomes a requirement, the fix is a Redis pub/sub "key revoked" message that
evicts the entry on all replicas. That's a small change and doesn't need a shorter TTL.

## Why misses are not cached
- A key created a second ago must work immediately.
- Caching misses would let someone spraying random keys fill the cache. Misses still cost a DB
  lookup. Rate limiting unauthenticated traffic (per IP, at nginx) is the right fix if that
  becomes a problem.
