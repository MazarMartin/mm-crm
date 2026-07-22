-- 0004_presented_responses.sql
-- Client swipe responses on presented properties.
--
-- A response ANNOTATES a presented property — it never moves or deletes it.
-- Staff keep seeing every presented item under the client's name; this table
-- adds the client's reaction (interested / passed + optional note) on top.
--
-- Keyed by (client_id, property_address), deliberately NOT FK'd to
-- presented_props.id: staff writes to mmPresented go through a delete-and-
-- reinsert adapter (replaceClientChildRows), so a CASCADE on presented_id
-- would wipe client responses every time staff touched the presented list.

CREATE TABLE IF NOT EXISTS presented_responses (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id        uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  property_address text NOT NULL,
  response         text NOT NULL CHECK (response IN ('interested','passed')),
  note             text NOT NULL DEFAULT '',
  responded_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (client_id, property_address)
);

CREATE INDEX IF NOT EXISTS idx_presented_responses_client
  ON presented_responses(client_id, responded_at DESC);

ALTER TABLE presented_responses ENABLE ROW LEVEL SECURITY;

-- Staff: full access via org membership on the parent client
-- (mirrors staff_full_presented_props from 0001).
DROP POLICY IF EXISTS "staff_full_presented_responses" ON presented_responses;
CREATE POLICY "staff_full_presented_responses" ON presented_responses
  FOR ALL TO authenticated
  USING      (EXISTS (SELECT 1 FROM clients c WHERE c.id = client_id AND is_org_staff(c.org_id)))
  WITH CHECK (EXISTS (SELECT 1 FROM clients c WHERE c.id = client_id AND is_org_staff(c.org_id)));

-- Clients: read + write their OWN responses only. No DELETE — changing your
-- mind is an UPDATE (upsert on the unique key); removal isn't a client action.
-- Helpers is_client_user() / jwt_client_name() come from migration 0003.
DROP POLICY IF EXISTS "client_read_own_responses" ON presented_responses;
CREATE POLICY "client_read_own_responses" ON presented_responses
  FOR SELECT TO authenticated
  USING (
    is_client_user()
    AND EXISTS (
      SELECT 1 FROM clients c
      WHERE c.id = client_id AND c.name = jwt_client_name() AND c.deleted_at IS NULL
    )
  );

DROP POLICY IF EXISTS "client_insert_own_responses" ON presented_responses;
CREATE POLICY "client_insert_own_responses" ON presented_responses
  FOR INSERT TO authenticated
  WITH CHECK (
    is_client_user()
    AND EXISTS (
      SELECT 1 FROM clients c
      WHERE c.id = client_id AND c.name = jwt_client_name() AND c.deleted_at IS NULL
    )
  );

DROP POLICY IF EXISTS "client_update_own_responses" ON presented_responses;
CREATE POLICY "client_update_own_responses" ON presented_responses
  FOR UPDATE TO authenticated
  USING (
    is_client_user()
    AND EXISTS (
      SELECT 1 FROM clients c
      WHERE c.id = client_id AND c.name = jwt_client_name() AND c.deleted_at IS NULL
    )
  )
  WITH CHECK (
    is_client_user()
    AND EXISTS (
      SELECT 1 FROM clients c
      WHERE c.id = client_id AND c.name = jwt_client_name() AND c.deleted_at IS NULL
    )
  );
