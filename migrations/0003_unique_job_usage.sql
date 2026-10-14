CREATE UNIQUE INDEX api_usage_job_unique_idx
ON api_usage (job_id)
WHERE job_id IS NOT NULL;
