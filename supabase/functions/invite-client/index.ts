// invite-client — Edge Function behind the "Send login invite" button.
//
// Why this exists: creating auth users needs the service-role key, which
// must never reach the browser. This function holds it server-side.
// Gerard clicks the button → browser calls this with his own session JWT →
// we verify he's staff → create/refresh the client's auth user → GoTrue
// sends the invite or magic-link email through the project's SMTP.
//
// Deploy:  supabase functions deploy invite-client --project-ref phkmwcimpyvmxbpdmuvw
// (JWT verification stays ON — default — so anonymous calls are rejected
//  at the gateway before this code even runs.)

import { createClient } from "npm:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  try {
    const url = Deno.env.get("SUPABASE_URL")!;
    const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

    // ── Verify the caller is a signed-in STAFF user ─────────────────────
    // The gateway already validated the JWT signature; here we resolve it
    // to a user and enforce the role check (clients must not invite).
    const authHeader = req.headers.get("Authorization") ?? "";
    const callerClient = createClient(url, anonKey, {
      global: { headers: { Authorization: authHeader } },
    });
    const { data: { user: caller }, error: callerErr } =
      await callerClient.auth.getUser();
    if (callerErr || !caller) return json({ error: "Not signed in" }, 401);
    if ((caller.user_metadata?.role ?? "staff") === "client") {
      return json({ error: "Client accounts cannot send invites" }, 403);
    }

    // ── Input ───────────────────────────────────────────────────────────
    // action: "invite" (default) creates/re-sends; "check" only reports
    // whether an auth account exists for the email, so the UI can tell
    // staff up-front instead of them guessing.
    const { email, client_name, action } = await req.json();
    if (!email || (!client_name && action !== "check")) {
      return json({ error: "email and client_name are required" }, 400);
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      return json({ error: "That doesn't look like a valid email" }, 400);
    }

    const admin = createClient(url, serviceKey);

    // Stamp the org onto the auth user so fetchOrgId can resolve it for
    // client-role sessions (they have no org_members row). Single-org
    // deployment, so first row is the org.
    const { data: orgs, error: orgErr } =
      await admin.from("orgs").select("id").limit(1);
    if (orgErr || !orgs?.length) {
      return json({ error: orgErr?.message ?? "no org found" }, 500);
    }
    const meta = { role: "client", client_name, org_id: orgs[0].id };

    // ── Existing user? Refresh metadata + re-send a magic link ─────────
    const { data: list, error: listErr } =
      await admin.auth.admin.listUsers({ perPage: 1000 });
    if (listErr) return json({ error: listErr.message }, 500);
    const existing = list.users.find(
      (u) => (u.email ?? "").toLowerCase() === String(email).toLowerCase(),
    );

    if (action === "check") {
      return json({
        ok: true,
        exists: !!existing,
        created_at: existing?.created_at ?? null,
        last_sign_in_at: existing?.last_sign_in_at ?? null,
        client_name: existing?.user_metadata?.client_name ?? null,
      });
    }

    if (existing) {
      const { error: updErr } = await admin.auth.admin.updateUserById(
        existing.id,
        { user_metadata: meta },
      );
      if (updErr) return json({ error: updErr.message }, 500);
      const { error: otpErr } = await admin.auth.signInWithOtp({
        email,
        options: { shouldCreateUser: false },
      });
      if (otpErr) return json({ error: otpErr.message }, 500);
      return json({ ok: true, status: "resent", message: "Login link re-sent" });
    }

    // ── New user → invite (creates the account AND sends the email) ────
    const { error: invErr } = await admin.auth.admin.inviteUserByEmail(
      email,
      { data: meta },
    );
    if (invErr) return json({ error: invErr.message }, 500);
    return json({ ok: true, status: "invited", message: "Invite sent" });
  } catch (e) {
    return json({ error: String((e as Error)?.message ?? e) }, 500);
  }
});
