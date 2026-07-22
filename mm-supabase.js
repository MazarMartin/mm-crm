// mm-supabase.js — Stage 2 integration block
//
// STEPS DONE:
//   1. login overlay + Supabase auth
//   2. mmCommissionUnlocked  → user_kv
//   3. mmCallStatus           → agent_calls (per-row upsert)
//   4. mmClientEdits          → clients.edits (diff-based upsert)
//   5. remaining keys wired (see USER_KV_KEYS + RELATIONAL_ADAPTERS below)
//
// Loaded as a module via `<script type="module" src="./mm-supabase.js"></script>`
// at the end of index.html.

// Self-hosted Supabase UMD bundle from ./vendor/supabase.js (loaded via a
// regular <script> tag in index.html). Exposes window.supabase with
// createClient. No CDN dependency, no ESM dynamic imports — works
// identically in Safari, Chrome, mobile, every browser. See the
// MM_SUPABASE_START comment block in index.html for the rationale.
const { createClient } = window.supabase;

const SUPABASE_URL = 'https://phkmwcimpyvmxbpdmuvw.supabase.co';
const SUPABASE_PUBLISHABLE_KEY = 'sb_publishable_aJddeid2D0-0kDWclrNRgQ_nt_kQih5';

// Keys that map 1:1 to a user_kv row (string in, string out). Adding a new
// per-user blob is just appending to this set — no per-key code required.
// Genuinely per-person UI state. Safe to differ between Gerard, Mon and
// Jeremy — nobody is harmed if their dismissed-news list or which week
// they're viewing doesn't match.
const USER_KV_KEYS = new Set([
  'mmCommissionUnlocked',
  'mmWeek',
  'mm_news_dismissed',
]);

// Shared business data — must be identical for every staff member. These
// lived in user_kv while everyone shared one login, which hid the problem;
// with individual staff accounts they'd silently diverge (Gerard marks a
// buyer HOT and Mon still sees Warm; Gerard adds an off-market and nobody
// else can see it). Stored org-wide in org_kv (migration 0005).
const ORG_KV_KEYS = new Set([
  'mmStatOverrides',
  'mmAutoMatches',
  'mmAutoMatchLastRun',
  'mmAutoMatchTs',
  'mmBuyerTemp',
  'mmBuyerTemps',
  'mmTemps',
  'mmNewSale',
  'mmNewOff',
]);

// Keys with bespoke adapters (write logic in RELATIONAL_ADAPTERS below,
// hydration logic in hydrateAll()).
const RELATIONAL_KEYS = new Set([
  'mmCallStatus',
  'mmCallComments',
  'mmClientEdits',
  'mmDeletedClients',
  'mmSavedMatches',
  'mmDismissedProps',
  'mmPresented',
  'mmClientResponses',
  'mmClientComments',
  'mmClientActivity',
  'mmPropComments',
  'mmForSaleEdits',
  'mmSoldEdits',
  'mmBlacklist',
  'mmDeletedFS',
]);

const MANAGED_KEYS = new Set([...USER_KV_KEYS, ...ORG_KV_KEYS, ...RELATIONAL_KEYS]);

// Module state
let supabase = null;
let currentUserId = null;
let currentOrgId = null;
let cache = {}; // {mmKey: localStorage-shaped string}

if (window._mmSupabaseInit) {
  console.warn('[MM-Supabase] already initialised, skipping');
} else {
  window._mmSupabaseInit = true;
  boot();
}

function boot() {
  supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);
  // Expose on window for console debugging. Not sensitive: the client
  // just holds the publishable/anon key which is already shipped in this
  // file. Being able to run `await window.mmSupabase.auth.getSession()`
  // from the console is worth it when auth flows misbehave.
  window.mmSupabase = supabase;
  window._mmSupabase = supabase;

  const overlay = buildOverlay();
  const statusEl = overlay.querySelector('#mm-sb-status');
  const form     = overlay.querySelector('#mm-sb-login');
  const errEl    = overlay.querySelector('#mm-sb-error');

  const setStatus = (msg, color) => {
    statusEl.textContent = msg || '';
    statusEl.style.color = color || '#1C3A2A';
    // Collapse the element when empty so the title doesn't sit miles away
    // from the form.
    statusEl.style.display = msg ? 'block' : 'none';
    statusEl.style.marginBottom = msg ? '20px' : '0';
  };
  const showError = (msg) => {
    errEl.textContent = msg;
    errEl.style.display = 'block';
  };

  supabase.auth.getSession().then(async ({ data: { session } }) => {
    if (session) {
      await afterLogin(session);
    } else {
      form.style.display = 'block';
    }
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errEl.style.display = 'none';
    setStatus('Sending link…');
    const email = overlay.querySelector('#mm-sb-email').value.trim();
    // shouldCreateUser:false is the security lock — if the email isn't
    // already a Supabase auth user Gerard has invited, no link is sent
    // and no account is auto-created. Random visitors typing a real
    // email get nothing. The UI response is identical either way so
    // there's no information leak about which emails are registered.
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: {
        shouldCreateUser: false,
        emailRedirectTo: window.location.origin + window.location.pathname,
      },
    });
    setStatus('');
    // We deliberately show the same "check your inbox" message whether
    // the email was found or not — probing for valid emails should tell
    // an attacker nothing. Real Supabase errors (network, rate limit)
    // still surface so legitimate users aren't left staring at nothing.
    if (error && !/user.*not.*found|invalid/i.test(error.message)) {
      showError(error.message);
      return;
    }
    form.style.display = 'none';
    overlay.querySelector('#mm-sb-sent').style.display = 'block';
  });

  async function afterLogin(session) {
    currentUserId = session.user.id;
    // Role & client mapping (foundation for the two-tier permission model).
    // Staff accounts (Gerard/Mon/Jeremy) log in with no user_metadata set —
    // they default to role='staff' so nothing breaks for existing users.
    // Client accounts get {role:'client', client_name:'…'} stamped on their
    // auth user by the "Send login invite" flow (see Edge Function).
    const meta = (session.user && session.user.user_metadata) || {};
    window.mmUserRole   = meta.role === 'client' ? 'client' : 'staff';
    window.mmUserClient = meta.client_name || '';
    window.mmUserEmail  = (session.user && session.user.email) || '';
    console.log('[MM-Supabase] session role:', window.mmUserRole,
                '| client:', window.mmUserClient || '(none)');
    setStatus('Loading…');
    try {
      currentOrgId = await fetchOrgId();
      cache = await hydrateAll();
      installLocalStorageOverrides();
      // Bypass the in-app password gate so the user doesn't see a second
      // login after ours. The existing app hides #mm-login-screen when
      // sessionStorage.mm_auth === '1'.
      sessionStorage.setItem('mm_auth', '1');
      const inAppGate = document.getElementById('mm-login-screen');
      if (inAppGate) inAppGate.classList.add('hidden');
      // The app built its tab bar at page load, BEFORE this login resolved —
      // so a client-role session still shows the staff tabs (clicks are
      // blocked by the showTab guard, but the buttons are visible). Rebuild
      // the bar now that the role is known, and land on the dashboard so
      // role-aware sections re-render with the right visibility.
      if (window.mmUserRole === 'client') {
        if (typeof window.buildTabBar === 'function') window.buildTabBar();
        if (typeof window.showTab === 'function') window.showTab('dashboard');
      }
      installSignOutButton();
      installFocusRehydrate();
      console.log('[MM-Supabase] hydrated cache:', cache);
      setTimeout(() => overlay.remove(), 200);
    } catch (e) {
      console.error('[MM-Supabase] init failed', e);
      showError('Init failed: ' + (e.message || e));
      setStatus('Connected (with errors) — see console.', '#C62828');
    }
  }
}

// ─── Login-time lookups ─────────────────────────────────────────────────

async function fetchOrgId() {
  // Client-role users aren't org_members. Resolve their org from the
  // org_id stamped into user_metadata at invite time, falling back to
  // their own clients row (readable under client_read_own_client_row,
  // migration 0003) for accounts invited before stamping existed.
  if (window.mmUserRole === 'client') {
    const { data: { session } } = await supabase.auth.getSession();
    const metaOrg = session && session.user && session.user.user_metadata
      ? session.user.user_metadata.org_id : null;
    if (metaOrg) return metaOrg;
    const { data, error } = await supabase
      .from('clients').select('org_id').limit(1).maybeSingle();
    if (error) throw error;
    if (!data) throw new Error('client account not linked to a client record');
    return data.org_id;
  }
  const { data, error } = await supabase
    .from('org_members')
    .select('org_id')
    .eq('user_id', currentUserId)
    .eq('role', 'staff')
    .limit(1)
    .maybeSingle();
  if (error) throw error;
  if (!data) throw new Error('no staff membership for this user');
  return data.org_id;
}

// ─── Bulk hydration ─────────────────────────────────────────────────────

async function hydrateAll() {
  const c = {};
  const [
    kv,
    calls,
    clientsRows,
    savedMatches,
    dismissed,
    presented,
    cliComments,
    cliActivity,
    propComments,
    manualListings, // currently unused — manual_listings tables stay empty until we migrate mmNewSale/Off proper
    listingEdits,
    presentedResponses,
    orgKv,
  ] = await Promise.all([
    supabase.from('user_kv').select('key, value').eq('user_id', currentUserId),
    supabase.from('agent_calls').select('agent_key, called, voicemail, comments').eq('org_id', currentOrgId),
    supabase.from('clients').select('id, name, edits, deleted_at').eq('org_id', currentOrgId),
    supabase.from('saved_matches').select('client_id, property_address, suburb, price, property_type, note, saved_at'),
    supabase.from('dismissed_props').select('client_id, property_address, dismissed_at'),
    supabase.from('presented_props').select('client_id, property_address, presented_at, data'),
    supabase.from('client_comments').select('client_id, body, created_at'),
    supabase.from('client_activity').select('client_id, kind, body, created_at'),
    supabase.from('property_comments').select('property_address, body, created_at').eq('org_id', currentOrgId),
    supabase.from('manual_listings').select('*').eq('org_id', currentOrgId),
    supabase.from('listing_edits').select('property_address, listing_type, edits, is_deleted, is_blacklisted').eq('org_id', currentOrgId),
    supabase.from('presented_responses').select('client_id, property_address, response, note, responded_at'),
    supabase.from('org_kv').select('key, value').eq('org_id', currentOrgId),
  ]);

  // Throw on any of them
  for (const r of [kv, calls, clientsRows, savedMatches, dismissed, presented, cliComments, cliActivity, propComments, manualListings, listingEdits, presentedResponses, orgKv]) {
    if (r.error) throw r.error;
  }

  // user_kv (per-person UI state) then org_kv (shared business data).
  // org_kv is applied second so it wins if a stale per-user row for a
  // now-org-scoped key is still sitting in user_kv from before migration
  // 0005 — the org value is the authoritative one.
  for (const row of kv.data || []) {
    c[row.key] = typeof row.value === 'string' ? row.value : JSON.stringify(row.value);
  }
  for (const row of orgKv.data || []) {
    c[row.key] = typeof row.value === 'string' ? row.value : JSON.stringify(row.value);
  }

  // Helper: name lookup by client_id (only for the org's non-deleted clients)
  const clientNameById = {};
  const liveClients = (clientsRows.data || []).filter((r) => r.deleted_at === null);
  for (const r of liveClients) clientNameById[r.id] = r.name;

  // mmClientEdits ← clients.edits (only non-empty)
  {
    const obj = {};
    for (const row of liveClients) {
      if (row.edits && Object.keys(row.edits).length > 0) obj[row.name] = row.edits;
    }
    if (Object.keys(obj).length > 0) c['mmClientEdits'] = JSON.stringify(obj);
  }

  // mmDeletedClients ← clients.deleted_at IS NOT NULL
  {
    const arr = (clientsRows.data || []).filter((r) => r.deleted_at !== null).map((r) => r.name);
    if (arr.length > 0) c['mmDeletedClients'] = JSON.stringify(arr);
  }

  // mmCallStatus + mmCallComments ← agent_calls
  {
    const statusObj = {};
    const commentsObj = {};
    for (const row of calls.data || []) {
      // Frontend (toggleAgentVM in index.html) reads/writes s.vm, not s.voicemail,
      // so expose the column under `vm` so the UI checkbox renders correctly.
      statusObj[row.agent_key] = { called: row.called, vm: row.voicemail };
      // saveAgentComment() in index.html stores a plain string per agent.
      // Accept any non-empty truthy value (string, array, or object).
      if (row.comments != null && row.comments !== '' &&
          !(Array.isArray(row.comments) && row.comments.length === 0)) {
        commentsObj[row.agent_key] = row.comments;
      }
    }
    if (Object.keys(statusObj).length > 0) c['mmCallStatus'] = JSON.stringify(statusObj);
    if (Object.keys(commentsObj).length > 0) c['mmCallComments'] = JSON.stringify(commentsObj);
  }

  // Helper: group rows by client name
  const groupByClientName = (rows, itemFn) => {
    const out = {};
    for (const row of rows) {
      const name = clientNameById[row.client_id];
      if (!name) continue;
      (out[name] = out[name] || []).push(itemFn(row));
    }
    return out;
  };

  // mmSavedMatches
  {
    const obj = groupByClientName(savedMatches.data || [], (r) => ({
      address: r.property_address,
      suburb: r.suburb,
      price: r.price,
      type: r.property_type,
      note: r.note,
      savedAt: r.saved_at,
    }));
    if (Object.keys(obj).length > 0) c['mmSavedMatches'] = JSON.stringify(obj);
  }

  // mmDismissedProps
  {
    const obj = groupByClientName(dismissed.data || [], (r) => r.property_address);
    if (Object.keys(obj).length > 0) c['mmDismissedProps'] = JSON.stringify(obj);
  }

  // mmPresented — hydrate the full JSONB `data` object (suburb, beds/baths,
  // price, heroPhoto, feedback…). Rows written by an older client that only
  // set property_address still round-trip: we fall back to a minimal
  // {address} object so the render doesn't break.
  {
    const obj = groupByClientName(presented.data || [], (r) => {
      const d = r.data && typeof r.data === 'object' ? r.data : {};
      if (!d.address) d.address = r.property_address;
      return d;
    });
    if (Object.keys(obj).length > 0) c['mmPresented'] = JSON.stringify(obj);
  }

  // mmClientResponses — client swipe reactions to presented properties.
  // Object-of-objects keyed by normalized address (not an array): the write
  // adapter upserts per (client, address), and clients have no DELETE right.
  {
    const obj = {};
    for (const r of (presentedResponses.data || [])) {
      const name = clientNameById[r.client_id];
      if (!name) continue;
      const addrKey = (r.property_address || '').toLowerCase().replace(/[^a-z0-9]/g, '');
      if (!addrKey) continue;
      (obj[name] = obj[name] || {})[addrKey] = {
        address: r.property_address,
        response: r.response,
        note: r.note || '',
        ts: r.responded_at ? Date.parse(r.responded_at) : 0,
      };
    }
    if (Object.keys(obj).length > 0) c['mmClientResponses'] = JSON.stringify(obj);
  }

  // mmClientComments
  {
    const obj = groupByClientName(cliComments.data || [], (r) => ({ body: r.body, createdAt: r.created_at }));
    if (Object.keys(obj).length > 0) c['mmClientComments'] = JSON.stringify(obj);
  }

  // mmClientActivity
  {
    const obj = groupByClientName(cliActivity.data || [], (r) => ({ kind: r.kind, ...r.body, createdAt: r.created_at }));
    if (Object.keys(obj).length > 0) c['mmClientActivity'] = JSON.stringify(obj);
  }

  // mmPropComments
  // Frontend (savePropertyComment in index.html) treats this as a flat
  // {key: stringValue} map (key = `clientName::addressNorm`), so hydrate
  // the same shape. If multiple rows exist per key, keep the latest body.
  {
    const obj = {};
    const seenAt = {};
    for (const r of propComments.data || []) {
      const t = new Date(r.created_at || 0).getTime();
      if (!(r.property_address in obj) || t >= (seenAt[r.property_address] || 0)) {
        obj[r.property_address] = r.body || '';
        seenAt[r.property_address] = t;
      }
    }
    if (Object.keys(obj).length > 0) c['mmPropComments'] = JSON.stringify(obj);
  }

  // listing_edits → split into mmForSaleEdits, mmSoldEdits, mmBlacklist, mmDeletedFS
  {
    const fs = {}, sold = {}, blacklist = [], deleted = [];
    for (const r of listingEdits.data || []) {
      if (r.is_blacklisted) blacklist.push(r.property_address);
      if (r.is_deleted) deleted.push(r.property_address);
      if (r.edits && Object.keys(r.edits).length > 0) {
        if (r.listing_type === 'forsale') fs[r.property_address] = r.edits;
        else if (r.listing_type === 'sold') sold[r.property_address] = r.edits;
      }
    }
    if (Object.keys(fs).length > 0)   c['mmForSaleEdits'] = JSON.stringify(fs);
    if (Object.keys(sold).length > 0) c['mmSoldEdits']    = JSON.stringify(sold);
    if (blacklist.length > 0)         c['mmBlacklist']    = JSON.stringify(blacklist);
    if (deleted.length > 0)           c['mmDeletedFS']    = JSON.stringify(deleted);
  }

  window._mmCache = c; // debug
  return c;
}

// ─── Helpers ────────────────────────────────────────────────────────────

async function resolveClientId(name, create = true) {
  const { data: existing, error: e1 } = await supabase
    .from('clients')
    .select('id')
    .eq('org_id', currentOrgId)
    .eq('name', name)
    .maybeSingle();
  if (e1) throw e1;
  if (existing) return existing.id;
  if (!create) return null;
  const { data: created, error: e2 } = await supabase
    .from('clients')
    .insert({ org_id: currentOrgId, name, created_by: currentUserId })
    .select('id')
    .single();
  if (e2) throw e2;
  return created.id;
}

// Generic "replace all child rows for parent" pattern used by per-client arrays.
async function replaceClientChildRows({ table, newValue, oldValue, itemToRow }) {
  // For each (name, items[]) in newValue: delete existing rows for that client,
  // then insert the new items.
  for (const [name, items] of Object.entries(newValue || {})) {
    if (!Array.isArray(items)) continue;
    const clientId = await resolveClientId(name, true);
    const { error: delErr } = await supabase.from(table).delete().eq('client_id', clientId);
    if (delErr) throw delErr;
    if (items.length > 0) {
      const rows = items.map((it) => itemToRow(it, clientId));
      const { error: insErr } = await supabase.from(table).insert(rows);
      if (insErr) throw insErr;
    }
  }
  // Clear child rows for clients that vanished from the map.
  const removedNames = Object.keys(oldValue || {}).filter((n) => !(n in (newValue || {})));
  for (const name of removedNames) {
    const clientId = await resolveClientId(name, false);
    if (clientId) {
      const { error: delErr } = await supabase.from(table).delete().eq('client_id', clientId);
      if (delErr) throw delErr;
    }
  }
}

async function userKvUpsert(key, value) {
  const { error } = await supabase.from('user_kv').upsert(
    { user_id: currentUserId, key, value },
    { onConflict: 'user_id,key' }
  );
  if (error) throw error;
}

async function userKvDelete(key) {
  const { error } = await supabase.from('user_kv').delete()
    .eq('user_id', currentUserId).eq('key', key);
  if (error) throw error;
}

// org_kv — same shape as user_kv but keyed by org, so every staff member
// reads and writes the same row. updated_by records who touched it last.
async function orgKvUpsert(key, value) {
  const { error } = await supabase.from('org_kv').upsert(
    { org_id: currentOrgId, key, value, updated_by: currentUserId, updated_at: new Date().toISOString() },
    { onConflict: 'org_id,key' }
  );
  if (error) throw error;
}

async function orgKvDelete(key) {
  const { error } = await supabase.from('org_kv').delete()
    .eq('org_id', currentOrgId).eq('key', key);
  if (error) throw error;
}

const parseObj = (s, fallback = {}) => { try { return JSON.parse(s || '{}') ?? fallback; } catch { return fallback; } };
const parseArr = (s, fallback = []) => { try { return JSON.parse(s || '[]') ?? fallback; } catch { return fallback; } };

// ─── Relational adapters ───────────────────────────────────────────────

const RELATIONAL_ADAPTERS = {
  mmCallStatus: {
    write: async (newStr) => {
      const obj = parseObj(newStr);
      const rows = Object.entries(obj).map(([agent_key, s]) => ({
        org_id: currentOrgId, agent_key,
        called: !!(s && s.called),
        // Accept both `vm` (the frontend's field name) and `voicemail` for safety.
        voicemail: !!(s && (s.vm !== undefined ? s.vm : s.voicemail)),
      }));
      if (rows.length === 0) return;
      const { error } = await supabase.from('agent_calls')
        .upsert(rows, { onConflict: 'org_id,agent_key' });
      if (error) throw error;
    },
    remove: async () => {
      const { error } = await supabase.from('agent_calls').delete().eq('org_id', currentOrgId);
      if (error) throw error;
    },
  },

  mmCallComments: {
    // Comments live in agent_calls.comments — same row as call status.
    // saveAgentComment() in index.html stores a string per agent (the textarea
    // value), so store whatever shape the frontend gave us.
    write: async (newStr) => {
      const obj = parseObj(newStr);
      const rows = Object.entries(obj).map(([agent_key, comments]) => ({
        org_id: currentOrgId, agent_key,
        comments: comments,
      }));
      if (rows.length === 0) return;
      // Upsert touches only the columns we send (preserves called/voicemail).
      const { error } = await supabase.from('agent_calls')
        .upsert(rows, { onConflict: 'org_id,agent_key' });
      if (error) throw error;
    },
    remove: async () => {
      // Clear the comments column on every row in this org.
      const { error } = await supabase.from('agent_calls')
        .update({ comments: [] }).eq('org_id', currentOrgId);
      if (error) throw error;
    },
  },

  mmClientEdits: {
    write: async (newStr, oldStr) => {
      const newValue = parseObj(newStr);
      const oldValue = parseObj(oldStr);
      const newNames = Object.keys(newValue);
      const removedNames = Object.keys(oldValue).filter((n) => !(n in newValue));
      if (newNames.length > 0) {
        const rows = newNames.map((name) => ({
          org_id: currentOrgId, name, edits: newValue[name] || {},
        }));
        const { error } = await supabase.from('clients')
          .upsert(rows, { onConflict: 'org_id,name' });
        if (error) throw error;
      }
      if (removedNames.length > 0) {
        const { error } = await supabase.from('clients')
          .update({ edits: {} })
          .eq('org_id', currentOrgId).in('name', removedNames);
        if (error) throw error;
      }
    },
    remove: async () => {
      const { error } = await supabase.from('clients')
        .update({ edits: {} }).eq('org_id', currentOrgId);
      if (error) throw error;
    },
  },

  mmDeletedClients: {
    write: async (newStr, oldStr) => {
      const newArr = parseArr(newStr);
      const oldArr = parseArr(oldStr);
      const toDelete = newArr.filter((n) => !oldArr.includes(n));
      const toRestore = oldArr.filter((n) => !newArr.includes(n));
      if (toDelete.length > 0) {
        const { error } = await supabase.from('clients')
          .update({ deleted_at: new Date().toISOString() })
          .eq('org_id', currentOrgId).in('name', toDelete).is('deleted_at', null);
        if (error) throw error;
      }
      if (toRestore.length > 0) {
        const { error } = await supabase.from('clients')
          .update({ deleted_at: null })
          .eq('org_id', currentOrgId).in('name', toRestore);
        if (error) throw error;
      }
    },
    remove: async () => {
      // Restore everyone
      const { error } = await supabase.from('clients')
        .update({ deleted_at: null }).eq('org_id', currentOrgId);
      if (error) throw error;
    },
  },

  mmSavedMatches: {
    write: async (newStr, oldStr) => {
      return replaceClientChildRows({
        table: 'saved_matches',
        newValue: parseObj(newStr),
        oldValue: parseObj(oldStr),
        itemToRow: (it, client_id) => ({
          client_id,
          property_address: it.address || '',
          suburb: it.suburb || null,
          price: it.price || null,
          property_type: it.type || null,
          note: it.note || null,
          saved_by: currentUserId,
          saved_at: it.savedAt || new Date().toISOString(),
        }),
      });
    },
    remove: async () => { /* handled by replaceClientChildRows on next write */ },
  },

  mmDismissedProps: {
    write: async (newStr, oldStr) => {
      return replaceClientChildRows({
        table: 'dismissed_props',
        newValue: parseObj(newStr),
        oldValue: parseObj(oldStr),
        itemToRow: (it, client_id) => ({
          client_id,
          property_address: typeof it === 'string' ? it : (it.address || ''),
          dismissed_by: currentUserId,
        }),
      });
    },
    remove: async () => {},
  },

  mmPresented: {
    write: async (newStr, oldStr) => {
      return replaceClientChildRows({
        table: 'presented_props',
        newValue: parseObj(newStr),
        oldValue: parseObj(oldStr),
        itemToRow: (it, client_id) => {
          // Full object goes into the `data` JSONB column so every field
          // (suburb, beds, baths, price, heroPhoto, feedback…) round-trips.
          // property_address stays as a top-level scalar so it can be
          // indexed and queried directly. If someone still hands us a
          // bare string (legacy write path), coerce to a minimal object.
          const obj = typeof it === 'object' && it ? it : { address: it || '' };
          const presentedAt = obj.presentedTs
            ? new Date(obj.presentedTs).toISOString()
            : (obj.presentedAt || new Date().toISOString());
          return {
            client_id,
            property_address: obj.address || '',
            presented_by: currentUserId,
            presented_at: presentedAt,
            data: obj,
          };
        },
      });
    },
    remove: async () => {},
  },

  mmClientResponses: {
    // Upsert-based, deliberately NOT replaceClientChildRows: clients have no
    // DELETE right on presented_responses (RLS, migration 0004), and replace
    // semantics would try one. Diff old vs new and upsert only what changed —
    // the unique (client_id, property_address) key makes re-answers updates.
    write: async (newStr, oldStr) => {
      const newV = parseObj(newStr);
      const oldV = parseObj(oldStr);
      for (const [name, respMap] of Object.entries(newV)) {
        if (!respMap || typeof respMap !== 'object') continue;
        const oldMap = (oldV && oldV[name]) || {};
        const changed = Object.entries(respMap).filter(([k, v]) =>
          JSON.stringify(v) !== JSON.stringify(oldMap[k]));
        if (!changed.length) continue;
        // create=false: a response can only attach to an existing client row;
        // client-role sessions couldn't insert into clients anyway.
        const clientId = await resolveClientId(name, false);
        if (!clientId) continue;
        const rows = changed.map(([, v]) => ({
          client_id: clientId,
          property_address: (v && v.address) || '',
          response: (v && v.response) || 'passed',
          note: (v && v.note) || '',
          responded_at: v && v.ts ? new Date(v.ts).toISOString() : new Date().toISOString(),
        })).filter((r) => r.property_address);
        if (!rows.length) continue;
        const { error } = await supabase.from('presented_responses')
          .upsert(rows, { onConflict: 'client_id,property_address' });
        if (error) throw error;
      }
    },
    remove: async () => {},
  },

  mmClientComments: {
    write: async (newStr, oldStr) => {
      return replaceClientChildRows({
        table: 'client_comments',
        newValue: parseObj(newStr),
        oldValue: parseObj(oldStr),
        itemToRow: (it, client_id) => ({
          client_id,
          body: typeof it === 'string' ? it : (it.body || ''),
          created_by: currentUserId,
          created_at: (it && it.createdAt) || new Date().toISOString(),
        }),
      });
    },
    remove: async () => {},
  },

  mmClientActivity: {
    write: async (newStr, oldStr) => {
      return replaceClientChildRows({
        table: 'client_activity',
        newValue: parseObj(newStr),
        oldValue: parseObj(oldStr),
        itemToRow: (it, client_id) => ({
          client_id,
          kind: (it && it.kind) || 'note',
          body: it || {},
          created_by: currentUserId,
          created_at: (it && it.createdAt) || new Date().toISOString(),
        }),
      });
    },
    remove: async () => {},
  },

  mmPropComments: {
    // savePropertyComment() in index.html stores a STRING per composite key
    // (`clientName::addressNorm`). One row per key, body = the string.
    write: async (newStr, oldStr) => {
      const newValue = parseObj(newStr);
      const oldValue = parseObj(oldStr);
      for (const [key, value] of Object.entries(newValue)) {
        const { error: delErr } = await supabase.from('property_comments')
          .delete().eq('org_id', currentOrgId).eq('property_address', key);
        if (delErr) throw delErr;
        const body = typeof value === 'string' ? value : (value && value.body) || '';
        if (body && body.trim()) {
          const { error } = await supabase.from('property_comments').insert({
            org_id: currentOrgId,
            property_address: key,
            body,
            created_by: currentUserId,
            created_at: new Date().toISOString(),
          });
          if (error) throw error;
        }
      }
      const removedKeys = Object.keys(oldValue).filter((k) => !(k in newValue));
      if (removedKeys.length > 0) {
        const { error } = await supabase.from('property_comments').delete()
          .eq('org_id', currentOrgId).in('property_address', removedKeys);
        if (error) throw error;
      }
    },
    remove: async () => {
      const { error } = await supabase.from('property_comments').delete().eq('org_id', currentOrgId);
      if (error) throw error;
    },
  },

  mmForSaleEdits: { write: (n, o) => writeListingEdits('forsale', n, o), remove: () => clearListingEditsType('forsale') },
  mmSoldEdits:    { write: (n, o) => writeListingEdits('sold',    n, o), remove: () => clearListingEditsType('sold')    },

  mmBlacklist: {
    write: async (newStr, oldStr) => writeListingFlag('is_blacklisted', 'forsale', newStr, oldStr),
    remove: async () => clearListingFlag('is_blacklisted'),
  },
  mmDeletedFS: {
    write: async (newStr, oldStr) => writeListingFlag('is_deleted', 'forsale', newStr, oldStr),
    remove: async () => clearListingFlag('is_deleted'),
  },
};

async function writeListingEdits(listingType, newStr, oldStr) {
  const newValue = parseObj(newStr);
  const oldValue = parseObj(oldStr);
  const newAddrs = Object.keys(newValue);
  const removedAddrs = Object.keys(oldValue).filter((a) => !(a in newValue));
  if (newAddrs.length > 0) {
    const rows = newAddrs.map((address) => ({
      org_id: currentOrgId,
      property_address: address,
      listing_type: listingType,
      edits: newValue[address] || {},
    }));
    const { error } = await supabase.from('listing_edits')
      .upsert(rows, { onConflict: 'org_id,property_address,listing_type' });
    if (error) throw error;
  }
  if (removedAddrs.length > 0) {
    const { error } = await supabase.from('listing_edits')
      .update({ edits: {} })
      .eq('org_id', currentOrgId).eq('listing_type', listingType).in('property_address', removedAddrs);
    if (error) throw error;
  }
}

async function clearListingEditsType(listingType) {
  const { error } = await supabase.from('listing_edits')
    .update({ edits: {} })
    .eq('org_id', currentOrgId).eq('listing_type', listingType);
  if (error) throw error;
}

async function writeListingFlag(flagCol, listingType, newStr, oldStr) {
  const newArr = parseArr(newStr);
  const oldArr = parseArr(oldStr);
  const toSet   = newArr.filter((a) => !oldArr.includes(a));
  const toClear = oldArr.filter((a) => !newArr.includes(a));
  if (toSet.length > 0) {
    const rows = toSet.map((address) => ({
      org_id: currentOrgId,
      property_address: address,
      listing_type: listingType,
      [flagCol]: true,
    }));
    const { error } = await supabase.from('listing_edits')
      .upsert(rows, { onConflict: 'org_id,property_address,listing_type' });
    if (error) throw error;
  }
  if (toClear.length > 0) {
    const { error } = await supabase.from('listing_edits')
      .update({ [flagCol]: false })
      .eq('org_id', currentOrgId).in('property_address', toClear);
    if (error) throw error;
  }
}

async function clearListingFlag(flagCol) {
  const { error } = await supabase.from('listing_edits')
    .update({ [flagCol]: false }).eq('org_id', currentOrgId);
  if (error) throw error;
}

// ─── Build the full WRITE_ADAPTERS map (user_kv + relational) ────────────

const WRITE_ADAPTERS = (() => {
  const m = {};
  for (const k of USER_KV_KEYS) {
    m[k] = {
      write: (v) => userKvUpsert(k, v),
      remove: () => userKvDelete(k),
    };
  }
  for (const k of ORG_KV_KEYS) {
    m[k] = {
      write: (v) => orgKvUpsert(k, v),
      remove: () => orgKvDelete(k),
    };
  }
  for (const [k, a] of Object.entries(RELATIONAL_ADAPTERS)) {
    m[k] = a;
  }
  return m;
})();

// ─── Refresh-on-focus ───────────────────────────────────────────────────
// Re-pull the Supabase state when the tab becomes visible again, so users
// who left the tab open see fresh data after returning. Throttled to avoid
// re-fetching on rapid focus/blur churn. The next time the app reads a
// managed key via lsGet() it'll see the updated cache; currently-rendered
// tabs won't auto-redraw, but the user gets fresh data on their next click
// or navigation.

const MIN_REHYDRATE_MS = 10_000; // don't re-hydrate more than once per 10s
const WRITE_SETTLE_MS  = 5_000;  // let recent writes reach Supabase first
let lastHydrateAt = 0;
let isRehydrating = false;
// Write-in-flight tracking. A re-hydrate replaces the whole cache with
// whatever Supabase returns — if a local write (e.g. "Present" saving to
// mmPresented) hasn't landed on the server yet, the re-hydrate would
// clobber it and the user's action silently vanishes. Track in-flight
// writes and skip re-hydrates until they've settled.
let pendingWrites = 0;
let lastWriteDoneTs = 0;

async function rehydrateNow(reason) {
  if (isRehydrating) return;
  if (!currentOrgId) return;
  if (Date.now() - lastHydrateAt < MIN_REHYDRATE_MS) return;
  if (pendingWrites > 0 || Date.now() - lastWriteDoneTs < WRITE_SETTLE_MS) {
    console.log('[MM-Supabase] re-hydrate skipped (' + reason + ') — local writes still settling');
    return;
  }
  isRehydrating = true;
  try {
    const fresh = await hydrateAll();
    // Replace cache entries individually so any keys not present in `fresh`
    // (e.g. user_kv default-empty) revert correctly.
    cache = fresh;
    lastHydrateAt = Date.now();
    console.log('[MM-Supabase] re-hydrated (' + reason + ')');
  } catch (e) {
    console.warn('[MM-Supabase] re-hydrate failed:', e);
  } finally {
    isRehydrating = false;
  }
}

function installFocusRehydrate() {
  lastHydrateAt = Date.now();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      rehydrateNow('tab-visible');
    }
  });
  window.addEventListener('focus', () => rehydrateNow('window-focus'));
  // Expose a manual hook so in-app refresh buttons (e.g. "Refresh Matches" on
  // the Clients tab) can pull fresh data from Supabase on demand. Bypasses
  // the 10s throttle since it's a deliberate user action.
  window.mmRehydrate = async function manualRehydrate(reason) {
    if (!currentOrgId) return;
    const saved = lastHydrateAt;
    lastHydrateAt = 0; // reset throttle for manual calls
    try {
      await rehydrateNow(reason || 'manual');
    } finally {
      if (lastHydrateAt === 0) lastHydrateAt = saved;
    }
  };
}

// ─── localStorage overrides ─────────────────────────────────────────────

function installLocalStorageOverrides() {
  // We override on Storage.prototype rather than the localStorage instance.
  // Safari (WebKit) refuses Object.defineProperty on instance methods of
  // Storage — the call silently fails, leaving every getItem reading from
  // actual localStorage and bypassing our hydrated cache. Modifying the
  // prototype works in all browsers. The `this === localStorage` guard
  // means sessionStorage and any other Storage instances are unaffected.
  const realGet = Storage.prototype.getItem;
  const realSet = Storage.prototype.setItem;
  const realRem = Storage.prototype.removeItem;

  Storage.prototype.getItem = function(key) {
    if (this === localStorage && MANAGED_KEYS.has(key)) {
      return Object.prototype.hasOwnProperty.call(cache, key) ? cache[key] : null;
    }
    return realGet.call(this, key);
  };

  Storage.prototype.setItem = function(key, value) {
    if (this === localStorage && MANAGED_KEYS.has(key)) {
      const oldStr = Object.prototype.hasOwnProperty.call(cache, key) ? cache[key] : null;
      const newStr = String(value);
      cache[key] = newStr;
      const a = WRITE_ADAPTERS[key];
      if (a && a.write) {
        pendingWrites++;
        a.write(newStr, oldStr).then(
          () => console.log(`[MM-Supabase] saved ${key}`),
          (e) => console.error(`[MM-Supabase] write failed for ${key}:`, e)
        ).finally(() => {
          pendingWrites = Math.max(0, pendingWrites - 1);
          lastWriteDoneTs = Date.now();
        });
      }
      return;
    }
    return realSet.call(this, key, value);
  };

  Storage.prototype.removeItem = function(key) {
    if (this === localStorage && MANAGED_KEYS.has(key)) {
      const oldStr = Object.prototype.hasOwnProperty.call(cache, key) ? cache[key] : null;
      delete cache[key];
      const a = WRITE_ADAPTERS[key];
      if (a && a.remove) {
        pendingWrites++;
        a.remove(oldStr).then(
          () => console.log(`[MM-Supabase] removed ${key}`),
          (e) => console.error(`[MM-Supabase] delete failed for ${key}:`, e)
        ).finally(() => {
          pendingWrites = Math.max(0, pendingWrites - 1);
          lastWriteDoneTs = Date.now();
        });
      }
      return;
    }
    return realRem.call(this, key);
  };

  console.log('[MM-Supabase] localStorage overrides installed for', [...MANAGED_KEYS]);
}

// ─── Sign-out button ─────────────────────────────────────────────────────

function installSignOutButton() {
  if (document.getElementById('mm-sb-signout')) return;
  const btn = document.createElement('button');
  btn.id = 'mm-sb-signout';
  btn.type = 'button';
  btn.textContent = 'SIGN OUT';
  btn.style.cssText = [
    'position:fixed', 'top:12px', 'right:12px', 'z-index:10000',
    'padding:8px 18px', 'font-size:10px', 'font-weight:600',
    'color:#F5F0E8', 'background:#1C3A2A',
    'border:1px solid #1C3A2A', 'border-radius:999px',
    'font-family:"DM Sans",system-ui,sans-serif', 'letter-spacing:1.5px',
    'cursor:pointer',
    'transition:background 0.15s',
    'box-shadow:0 2px 6px rgba(28,58,42,0.18)',
  ].join(';');
  btn.addEventListener('mouseenter', () => { btn.style.background = '#2D5A40'; });
  btn.addEventListener('mouseleave', () => { btn.style.background = '#1C3A2A'; });
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'SIGNING OUT…';
    try {
      await supabase.auth.signOut();
      location.reload();
    } catch (e) {
      console.error('[MM-Supabase] sign out failed', e);
      btn.disabled = false;
      btn.textContent = 'SIGN OUT';
    }
  });
  document.body.appendChild(btn);
}

// ─── Login overlay UI ───────────────────────────────────────────────────

function buildOverlay() {
  const o = document.createElement('div');
  o.id = 'mm-supabase-overlay';
  // Match the original in-app login: cream gradient, Montserrat body font.
  o.style.cssText = [
    'position:fixed', 'inset:0', 'z-index:99999',
    'background:linear-gradient(145deg,#F5F0E8 0%,#EDE5D5 60%,#FBF7EE 100%)',
    'display:flex', 'align-items:center', 'justify-content:center',
    'font-family:"Montserrat",system-ui,sans-serif',
  ].join(';');

  // Reuse the app's existing .login-box / .login-input / .login-btn /
  // .login-error CSS classes so the overlay inherits the exact same visual
  // identity as the original in-app login screen (and stays in sync if the
  // app's stylesheet ever changes).
  o.innerHTML = `
    <div class="login-box">
      <div style="margin-bottom:28px;text-align:center;">
        <div style="font-family:'Times New Roman',Georgia,serif;font-size:clamp(22px,6vw,30px);font-weight:700;color:#1C3A2A;letter-spacing:1.5px;line-height:1.1;white-space:nowrap;">MAZAR MARTIN</div>
        <div style="font-family:'DM Sans',system-ui,sans-serif;font-size:clamp(9px,2.5vw,12px);font-weight:500;color:#C9A84C;letter-spacing:2.5px;margin-top:6px;white-space:nowrap;">BUYERS ADVISORY</div>
      </div>
      <div id="mm-sb-status" style="font-family:'DM Sans',system-ui,sans-serif;font-size:12px;color:#1C3A2A;text-align:center;letter-spacing:0.5px;display:none;"></div>
      <form id="mm-sb-login" style="display:none;">
        <input id="mm-sb-email" type="email" class="login-input" placeholder="Email" required autocomplete="email" style="letter-spacing:0;">
        <button type="submit" class="login-btn">Send login link</button>
      </form>
      <div id="mm-sb-sent" style="display:none;font-family:'DM Sans',system-ui,sans-serif;font-size:13px;color:#1C3A2A;text-align:center;line-height:1.6;margin-top:12px;padding:14px 16px;background:rgba(28,58,42,0.06);border-radius:8px;">
        If that email is registered, we've sent you a login link.<br>
        <span style="font-size:11px;color:rgba(28,58,42,0.65)">Check your inbox and click the link to sign in.</span>
      </div>
      <div id="mm-sb-error" class="login-error" style="display:none;"></div>
    </div>
  `;
  document.body.appendChild(o);
  return o;
}
