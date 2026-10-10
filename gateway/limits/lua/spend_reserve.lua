-- Reserve worst-case cost against a key's monthly spend cap. Amounts are integer nano-USD.
-- KEYS[1] settled spend, KEYS[2] reservations hash (request id -> amount),
-- KEYS[3] reservation expiry zset (request id -> expires at ms), KEYS[4] total reserved
-- ARGV[1] cap, ARGV[2] request id, ARGV[3] amount, ARGV[4] reservation TTL ms, ARGV[5] key TTL ms
-- Returns {status, spent, reserved}: 1 = reserved, 0 = would exceed cap,
--   -1 = settled spend unknown in Redis (caller hydrates it from Postgres and retries)
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local spent = redis.call('GET', KEYS[1])
if not spent then
  return {-1, 0, 0}
end
spent = tonumber(spent)

-- Release reservations whose request died without settling (crash, OOM, deploy).
local stale = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now)
for _, id in ipairs(stale) do
  local amount = tonumber(redis.call('HGET', KEYS[2], id) or '0')
  redis.call('HDEL', KEYS[2], id)
  redis.call('ZREM', KEYS[3], id)
  redis.call('DECRBY', KEYS[4], amount)
end

local reserved = tonumber(redis.call('GET', KEYS[4]) or '0')
local amount = tonumber(ARGV[3])
if spent + reserved + amount > tonumber(ARGV[1]) then
  return {0, spent, reserved}
end

redis.call('HSET', KEYS[2], ARGV[2], amount)
redis.call('ZADD', KEYS[3], now + tonumber(ARGV[4]), ARGV[2])
redis.call('INCRBY', KEYS[4], amount)
for i = 1, 4 do
  redis.call('PEXPIRE', KEYS[i], ARGV[5])
end
return {1, spent, reserved + amount}
