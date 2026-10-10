-- Token-bucket rate limit for one API key: requests/minute and tokens/minute, checked together.
-- KEYS[1] rpm bucket, KEYS[2] tpm bucket (hashes: tokens, ts)
-- ARGV[1] rpm limit (0 = unlimited), ARGV[2] tpm limit (0 = unlimited),
-- ARGV[3] tokens this request may use (estimate)
-- Returns {allowed (0/1), rpm_remaining, tpm_remaining, retry_after_ms}
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- A bucket holds up to `limit` tokens and refills at `limit` per 60s.
-- Clocks can jump backwards (NTP step, VM time sync): elapsed time is clamped at 0 and the
-- stored timestamp never moves back, so a jump only pauses refill instead of removing tokens.
local last_ts = {}

local function load(key, limit)
  local v = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens, ts = tonumber(v[1]), tonumber(v[2])
  if tokens == nil then
    last_ts[key] = now
    return limit
  end
  last_ts[key] = math.max(ts, now)
  return math.min(limit, tokens + math.max(0, now - ts) * limit / 60000)
end

local function save(key, tokens)
  redis.call('HSET', key, 'tokens', tokens, 'ts', last_ts[key])
  redis.call('PEXPIRE', key, 120000)
end

local rpm, tpm, want = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local rpm_tokens = rpm > 0 and load(KEYS[1], rpm) or 0
local tpm_tokens = tpm > 0 and load(KEYS[2], tpm) or 0
-- A request larger than the whole TPM bucket is admitted once the bucket is full (it then
-- goes into debt) instead of being rejected forever.
local need_tpm = math.min(want, tpm)

local wait_ms = 0
if rpm > 0 and rpm_tokens < 1 then
  wait_ms = math.max(wait_ms, (1 - rpm_tokens) * 60000 / rpm)
end
if tpm > 0 and tpm_tokens < need_tpm then
  wait_ms = math.max(wait_ms, (need_tpm - tpm_tokens) * 60000 / tpm)
end
if wait_ms > 0 then
  return {0, math.floor(rpm_tokens), math.floor(tpm_tokens), math.ceil(wait_ms)}
end

if rpm > 0 then
  rpm_tokens = rpm_tokens - 1
  save(KEYS[1], rpm_tokens)
end
if tpm > 0 then
  tpm_tokens = tpm_tokens - want
  save(KEYS[2], tpm_tokens)
end
return {1, math.floor(rpm_tokens), math.floor(tpm_tokens), 0}
