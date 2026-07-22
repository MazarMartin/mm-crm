-- 0005_org_kv.sql
-- Org-scoped key/value store, so shared business data stops being
-- per-user.
--
-- Why: user_kv is PRIMARY KEY (user_id, key) — strictly per-person. That
-- was invisible while all staff shared one login, but the moment Gerard,
-- Mon and Jeremy get individual accounts these would silently diverge:
--
--   mmBuyerTemp / mmBuyerTemps / mmTemps  → Hot/Warm/Cold on every buyer
--   mmNewSale / mmNewOff                  → manually added properties
--                                           ("+ Add Property", "Add Lead")
--   mmStatOverrides                       → dashboard stat overrides
--   mmAutoMatches / mmAutoMatchLastRun / mmAutoMatchTs → computed matches
--
-- Gerard adding an off-market that Mon never sees is exactly the class of
-- bug the Supabase migration was meant to end. These move to org scope.
-- Genuinely per-person UI state (mmWeek, mm_news_dismissed,
-- mmCommissionUnlocked) deliberately stays in user_kv.

CREATE TABLE IF NOT EXISTS org_kv (
  org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  key        text NOT NULL,
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES auth.users(id),
  PRIMARY KEY (org_id, key)
);

ALTER TABLE org_kv ENABLE ROW LEVEL SECURITY;

-- Staff-only: clients have no business reading org-wide operational state.
DROP POLICY IF EXISTS "staff_full_org_kv" ON org_kv;
CREATE POLICY "staff_full_org_kv" ON org_kv
  FOR ALL TO authenticated
  USING      (is_org_staff(org_id))
  WITH CHECK (is_org_staff(org_id));

-- Backfill: lift the existing values out of whichever user account has
-- them (today that's the single shared login) so nothing is lost on
-- cutover. Picks the most recently updated row per key if more than one
-- account somehow has it. Idempotent — ON CONFLICT DO NOTHING means
-- re-running never clobbers newer org-level edits.
INSERT INTO org_kv (org_id, key, value, updated_at)
SELECT om.org_id,
       uk.key,
       uk.value,
       uk.updated_at
FROM user_kv uk
JOIN org_members om ON om.user_id = uk.user_id AND om.role = 'staff'
WHERE uk.key IN (
        'mmBuyerTemp','mmBuyerTemps','mmTemps',
        'mmNewSale','mmNewOff',
        'mmStatOverrides',
        'mmAutoMatches','mmAutoMatchLastRun','mmAutoMatchTs'
      )
  AND uk.updated_at = (
        SELECT MAX(uk2.updated_at)
        FROM user_kv uk2
        JOIN org_members om2 ON om2.user_id = uk2.user_id AND om2.role = 'staff'
        WHERE uk2.key = uk.key AND om2.org_id = om.org_id
      )
ON CONFLICT (org_id, key) DO NOTHING;
