-- Record a successful upstream call.
-- KEYS[1] failures zset, KEYS[2] open_until (ms), KEYS[3] half-open probe lock
-- Returns: 1 if this success closed a half-open circuit, else 0
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local open_until = tonumber(redis.call('GET', KEYS[2]))
-- Half-open = cooldown elapsed, or a probe is in flight. Checking the probe lock too means a
-- clock that steps backwards after the probe was granted can't leave the circuit stuck.
if open_until and (now >= open_until or redis.call('EXISTS', KEYS[3]) == 1) then
  redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
  return 1
end
return 0
