-- Usage above a newly applied or lowered limit starts a grace period; the clock is recorded once.
ALTER TABLE licensing_state ADD COLUMN IF NOT EXISTS over_limit_since timestamptz;
-- The Free plan is generous enough to evaluate the product on one project (it was 1 machine and 2 AI accounts).
ALTER TABLE licensing_state ALTER COLUMN plan_limits SET DEFAULT '{"free":{"machines":3,"ai_accounts":6,"projects":3,"users":3},"team":{"machines":5,"ai_accounts":12,"projects":25,"users":25},"business":{"machines":50,"ai_accounts":100,"projects":250,"users":250}}'::jsonb;
UPDATE licensing_state SET plan_limits = jsonb_set(plan_limits, '{free}', '{"machines":3,"ai_accounts":6,"projects":3,"users":3}'::jsonb)
 WHERE plan_limits->'free' = '{"machines":1,"ai_accounts":2,"projects":3,"users":3}'::jsonb;
