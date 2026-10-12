-- Settle a reservation: release what was reserved, record what was actually spent.
-- KEYS as in spend_reserve.lua; ARGV[1] request id, ARGV[2] actual amount (nano-USD)
-- Returns the settled spend, -1 if the total is unknown in Redis, or -2 if there was no
-- reservation to settle (already settled).
--
-- Idempotent: the cost is added only together with removing the reservation, so a retry after
-- an ambiguous failure (timeout after Redis ran the script) can't count the cost twice.
-- Reservations outlive the request (spend_reservation_ttl_s > total timeout + retry window),
-- so a live request's reservation is always still there to settle.
local amount = redis.call('HGET', KEYS[2], ARGV[1])
if not amount then
  return -2
end
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('DECRBY', KEYS[4], amount)
-- Only add to a hydrated total. If the key is missing (e.g. Redis restarted), the next
-- reserve() rebuilds it from Postgres, which already includes this request's cost.
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[2])
end
return -1
