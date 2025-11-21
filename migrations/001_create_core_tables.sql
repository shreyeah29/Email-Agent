-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
-- Enable trigram for fuzzy text search
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Vendors table
CREATE TABLE vendors (
  vendor_id SERIAL PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  aliases TEXT[],
  meta JSONB,
  created_at TIMESTAMP DEFAULT now()
);

-- Projects table
CREATE TABLE projects (
  project_id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  codes TEXT[],
  meta JSONB,
  created_at TIMESTAMP DEFAULT now()
);

-- Invoices table (flexible JSONB core)
CREATE TABLE invoices (
  invoice_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_email_id TEXT,
  created_at TIMESTAMP DEFAULT now(),
  raw_email_s3 TEXT,        -- link to saved original message JSON
  attachments JSONB,        -- [{filename,url,type}]
  raw_text TEXT,            -- combined extracted text (email + OCR)
  extracted JSONB,          -- { field_name: {value:, confidence:, provenance: {...}}, ... }
  normalized JSONB,         -- canonical resolved fields: { vendor_id:, project_id:, total_amount:, currency:, date: ... }
  tags TEXT[],              -- user-defined tags
  extractor_version TEXT,
  reconciliation_status TEXT, -- 'auto_matched'|'needs_review'|'manual'
  extra JSONB               -- reserved for future structured columns
);

-- Audit trail for invoice edits
CREATE TABLE invoice_audit (
  audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id UUID REFERENCES invoices(invoice_id) ON DELETE CASCADE,
  field_name TEXT,
  old_value TEXT,
  new_value TEXT,
  user_name TEXT,
  changed_at TIMESTAMP DEFAULT now(),
  meta JSONB
);

-- Indexes for performance
CREATE INDEX invoices_extracted_gin_idx ON invoices USING GIN (extracted);
CREATE INDEX invoices_normalized_gin_idx ON invoices USING GIN (normalized);
CREATE INDEX invoices_tags_gin_idx ON invoices USING GIN (tags);
CREATE INDEX invoices_raw_text_trgm ON invoices USING GIN (to_tsvector('english', raw_text));
CREATE INDEX invoices_reconciliation_status_idx ON invoices(reconciliation_status);
CREATE INDEX invoices_created_at_idx ON invoices(created_at);
CREATE INDEX invoice_audit_invoice_id_idx ON invoice_audit(invoice_id);

-- Training examples table (optional, for ML improvements)
CREATE TABLE training_examples (
  example_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id UUID REFERENCES invoices(invoice_id) ON DELETE SET NULL,
  original_extracted JSONB,
  corrected_extracted JSONB,
  corrected_normalized JSONB,
  created_at TIMESTAMP DEFAULT now(),
  user_name TEXT
);

