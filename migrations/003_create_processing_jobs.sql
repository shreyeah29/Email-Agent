-- Processing jobs table for candidate message review workflow
-- Additive migration — does not modify existing tables

CREATE TABLE IF NOT EXISTS processing_jobs (
  job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', -- queued, processing, success, failed
  progress INTEGER DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
  queued_at TIMESTAMP DEFAULT now(),
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  result_path TEXT,
  result JSONB,
  error_message TEXT,
  created_at TIMESTAMP DEFAULT now()
);

-- Indexes for job lookup
CREATE INDEX IF NOT EXISTS processing_jobs_message_id_idx ON processing_jobs(message_id);
CREATE INDEX IF NOT EXISTS processing_jobs_status_idx ON processing_jobs(status);
CREATE INDEX IF NOT EXISTS processing_jobs_queued_at_idx ON processing_jobs(queued_at);

-- Unique constraint to prevent duplicate processing (idempotency)
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_message_id_status_idx 
  ON processing_jobs(message_id) 
  WHERE status = 'success';

