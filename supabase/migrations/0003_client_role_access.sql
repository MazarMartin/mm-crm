-- 0003_client_role_access.sql
-- RLS for client-role logins (the buyer portal).
--
-- Client users are NOT org_members — they're auth users carrying
-- {role:'client', client_name:'…'} in user_metadata (stamped at invite by
-- create_client_user.py / the invite-client Edge Function). Their portal
-- view needs exactly two reads:
--   1. their own `clients` row (budget / locations / prefs for "My File")
--   2. their own `presented_props` rows ("Properties Presented to You")
-- Everything else stays invisible: policies are permissive (OR'd), so
-- these grants add to the staff policies without widening them, and no
-- write access is granted to clients at all.

CREATE OR REPLACE FUNCTION jwt_client_name() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(auth.jwt() -> 'user_metadata' ->> 'client_name', '')
$$;

CREATE OR REPLACE FUNCTION is_client_user() RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(auth.jwt() -> 'user_metadata' ->> 'role', '') = 'client'
$$;

DROP POLICY IF EXISTS "client_read_own_client_row" ON clients;
CREATE POLICY "client_read_own_client_row" ON clients
  FOR SELECT TO authenticated
  USING (is_client_user() AND name = jwt_client_name() AND deleted_at IS NULL);

DROP POLICY IF EXISTS "client_read_own_presented" ON presented_props;
CREATE POLICY "client_read_own_presented" ON presented_props
  FOR SELECT TO authenticated
  USING (
    is_client_user()
    AND EXISTS (
      SELECT 1 FROM clients c
      WHERE c.id = client_id
        AND c.name = jwt_client_name()
        AND c.deleted_at IS NULL
    )
  );
