-- Record one distilled request's outcome and roll back atomically if quality dropped.
-- KEYS[1] task hash (state, weight, ...), KEYS[2] per-minute counters hash (field "<min>:n" / "<min>:f")
-- ARGV[1] failed (0/1), ARGV[2] window minutes, ARGV[3] min samples, ARGV[4] failure threshold,
-- ARGV[5] z (1.96 for a 95% Wilson bound)
-- Returns {rolled_back (0/1), n, failures} over the window.
--
-- Rollback rule: n >= min_samples AND the Wilson lower bound of the failure rate > threshold,
-- i.e. we're confident the true failure rate is above the threshold, not just unlucky. Done in
-- one script, so concurrent replicas can't both decide (or half-apply) a rollback.
local t = redis.call('TIME')
local minute = math.floor(tonumber(t[1]) / 60)
redis.call('HINCRBY', KEYS[2], minute .. ':n', 1)
if ARGV[1] == '1' then
  redis.call('HINCRBY', KEYS[2], minute .. ':f', 1)
end
redis.call('EXPIRE', KEYS[2], (tonumber(ARGV[2]) + 2) * 60)

local n, f = 0, 0
for m = minute - tonumber(ARGV[2]) + 1, minute do
  n = n + tonumber(redis.call('HGET', KEYS[2], m .. ':n') or '0')
  f = f + tonumber(redis.call('HGET', KEYS[2], m .. ':f') or '0')
end

local state = redis.call('HGET', KEYS[1], 'state')
if (state == 'canary' or state == 'full') and n >= tonumber(ARGV[3]) then
  local z = tonumber(ARGV[5])
  local p = f / n
  local denom = 1 + z * z / n
  local center = p + z * z / (2 * n)
  local margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
  local lower = (center - margin) / denom
  if lower > tonumber(ARGV[4]) then
    redis.call('HSET', KEYS[1], 'state', 'rolled_back', 'weight', '0')
    return {1, n, f}
  end
end
return {0, n, f}
