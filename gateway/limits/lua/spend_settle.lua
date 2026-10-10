-- Settle a reservation: release what was reserved, record what was actually spent.
-- KEYS as in spend_reserve.lua; ARGV[1] request id, ARGV[2] actual amount (nano-USD)
-- Returns the settled spend (-1 if unknown in Redis)
local amount = redis.call('HGET', KEYS[2], ARGV[1])
if amount then
  redis.call('HDEL', KEYS[2], ARGV[1])
  redis.call('ZREM', KEYS[3], ARGV[1])
  redis.call('DECRBY', KEYS[4], amount)
end
-- Only add to a hydrated total. If the key is missing (e.g. Redis restarted), the next
-- reserve() rebuilds it from Postgres, which already includes this request's cost.
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[2])
end
return -1
