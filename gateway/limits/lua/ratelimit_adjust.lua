-- Correct a TPM bucket once the real token count is known.
-- KEYS[1] tpm bucket; ARGV[1] tpm limit; ARGV[2] delta (positive = refund, negative = debit)
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local limit, delta = tonumber(ARGV[1]), tonumber(ARGV[2])
local v = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens, ts = tonumber(v[1]), tonumber(v[2])
if tokens == nil then
  tokens, ts = limit, now
else
  -- clamp: see ratelimit_acquire.lua (clocks can jump backwards)
  tokens = math.min(limit, tokens + math.max(0, now - ts) * limit / 60000)
  ts = math.max(ts, now)
end
tokens = math.min(limit, tokens + delta)
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts)
redis.call('PEXPIRE', KEYS[1], 120000)
return math.floor(tokens)
