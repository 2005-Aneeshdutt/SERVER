-- Record a failed upstream call.
-- KEYS[1] failures zset, KEYS[2] open_until (ms), KEYS[3] half-open probe lock
-- ARGV[1] threshold, ARGV[2] window (ms), ARGV[3] cooldown (ms), ARGV[4] unique member,
-- ARGV[5] TTL for the open_until key (ms)
-- Returns: 1 if the circuit is open after this call, else 0
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local open_until = tonumber(redis.call('GET', KEYS[2]))
if open_until then
  if now >= open_until or redis.call('EXISTS', KEYS[3]) == 1 then
    -- half-open and the probe (or a straggler) failed: re-open for another cooldown
    redis.call('SET', KEYS[2], now + tonumber(ARGV[3]), 'PX', ARGV[5])
    redis.call('DEL', KEYS[3])
  end
  return 1
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - tonumber(ARGV[2]))
redis.call('PEXPIRE', KEYS[1], ARGV[2])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
  redis.call('SET', KEYS[2], now + tonumber(ARGV[3]), 'PX', ARGV[5])
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
