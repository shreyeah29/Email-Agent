-- Seed sample vendors
INSERT INTO vendors (canonical_name, aliases, meta) VALUES
  ('ACME Supplies Pvt Ltd', ARRAY['ACME', 'Acme Supplies', 'ACME Supplies'], '{"category": "supplies"}'),
  ('TechCorp Solutions', ARRAY['TechCorp', 'Tech Corp Solutions'], '{"category": "technology"}'),
  ('Office Depot Inc', ARRAY['Office Depot', 'OfficeDepot'], '{"category": "office_supplies"}'),
  ('Global Services LLC', ARRAY['Global Services', 'GlobalServ'], '{"category": "services"}');

-- Seed sample projects
INSERT INTO projects (name, codes, meta) VALUES
  ('Project Alpha', ARRAY['ALPHA', 'PROJ-ALPHA'], '{"client": "Client A"}'),
  ('Project Beta', ARRAY['BETA', 'PROJ-BETA'], '{"client": "Client B"}'),
  ('Infrastructure Upgrade', ARRAY['INFRA', 'INFRA-UPG'], '{"client": "Internal"}'),
  ('Marketing Campaign 2025', ARRAY['MKT-2025', 'MARKETING'], '{"client": "Marketing Dept"}');

