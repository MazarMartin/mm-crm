-- 0002_presented_props_data.sql
-- Widen presented_props so per-property metadata (suburb, beds/baths, price,
-- heroPhoto, feedback, etc.) round-trips through Supabase. Before this the
-- table only stored client_id + property_address, so every hydration wiped
-- the presented card back to a bare address string — the client-facing
-- "Properties Presented to You" view had no data to render.
--
-- We use a JSONB column rather than adding one column per field so future
-- additions (photo variants, notes, videos) don't need more migrations.
-- Existing rows get an empty object as their default so nothing breaks.

ALTER TABLE presented_props
  ADD COLUMN IF NOT EXISTS data JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Backfill any rows that already exist with a minimal object shape.
UPDATE presented_props
   SET data = jsonb_build_object('address', property_address)
 WHERE data = '{}'::jsonb;
