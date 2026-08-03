# FinTalent joins the shared project-monitor schema.
# Remote verification (2026-08-04) confirmed these already exist:
#   public.projects
#   public.scraper_runs
#   public.email_attempts
#   public.scraper_sessions
#   acquire_scraper_worker_lock / renew_scraper_worker_lock / release_scraper_worker_lock
#   platform_category_extraction_status allows NOT_EXPOSED
#
# This migration is intentionally idempotent and does not alter
# already-applied shared objects. It only documents FinTalent readiness
# and ensures a scraper_sessions row can be created for platform=fintalent
# without conflicting with other platforms.

-- Ensure scraper_sessions accepts fintalent platform rows (no platform check constraint change needed)
DO $$
BEGIN
  -- Soft documentation marker only; no destructive changes.
  RAISE NOTICE 'FinTalent monitor uses shared tables; no schema delta required.';
END $$;
