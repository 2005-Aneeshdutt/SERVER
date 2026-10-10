-- Circuit breaker admission check. Uses Redis' clock so all replicas agree on time.
-- KEYS[1] failures zset, KEYS[2] open_until (ms), KEYS[3] half-open probe lock
-- ARGV[1] probe lock TTL (ms)
-- Returns: 1 = closed (allow), 2 = half-open and this caller is the probe, 0 = reject
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local open_until = tonumber(redis.call('GET', KEYS[2]))
if not open_until then
  return 1
end
if now < open_until then
  return 0
end
if redis.call('SET', KEYS[3], '1', 'NX', 'PX', ARGV[1]) then
  return 2
end
return 0
