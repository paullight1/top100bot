# AFL bot knowledge and announcements administration

User requests (2026-10-17): make replies more helpful with fewer admin notifications;
allow Top100 admins to load knowledge and trigger announcements from the admin page.
The user has instructed that plans are approved for autonomous implementation.

## Delivered conversation change
Direct greetings/testing/capabilities respond immediately. Ambiguous or unsupported
requests get a clarification, not automatic admin forwarding. Official facts remain
validated accessible source excerpts; chat only resolves references. Explicit human
handoffs are throttled per sender/chat. Operational alerts remain.

## Admin integration
Add /admin/afl-bot inside the existing Top100 shell, protected by its existing
requireAdmin guard. All browser requests use the same-origin admin API. A server-only
bridge credential authenticates calls to the bot's optional private HTTP API. Never
send Top100 Supabase credentials or member profiles to the bot. No Top100 schema changes.

The bot API is disabled by default (port zero/no key). Bind to loopback by default;
Docker deployments can use a private network binding without publishing a host port.
Enabling or exposing the bridge requires deliberate connection configuration after
the code is reviewable. Do not print or inspect existing environment secrets.

Knowledge uses the current bot SQLite catalog: text or readable PDF creates a draft,
default audience admin_only. Admins inspect full text and choose members before
publishing; publication is explicit, with effective/expiry dates and optional
predecessor for replacement. Published entries remain immutable; retiring removes
answer/download eligibility. Do not automatically ingest announcements as knowledge.

Announcements are persisted separately alongside the knowledge/session data. Drafts
capture literal admin-authored text and the exact configured test-group destination.
Preview yields an actor-bound short-lived one-use confirmation. Send requires that
preview, unchanged content/destination and current allowlist. No wildcard targets,
member list, automatic scheduling, or implicit sends. Atomically claim sending;
record sent or delivery-unconfirmed. Never automatically retry an uncertain send.
Preserve audit actors/timestamps and the existing volume.

## UI
Use existing Top100 PageHeader, neutral panels, orange/yellow primary action and
accessible labels. Tabs: knowledge and announcements. Explain draft/published and
private/member audience beside controls. Show setup/unavailable/loading/empty/error
states, preserve inputs after failure, and support mobile stacked lists. Include
bot connection status without exposing credentials or personal phone numbers.

## Verification
Bot tests: auth/no-key fail closed, private drafts, PDF parsing, publication lifecycle,
exact test-group only, expired/wrong-actor previews, duplicate send prevention,
uncertain delivery and actor audit. Top100 tests: requireAdmin, same-origin mutations,
fixed action/path mapping, server-only token, safe proxy errors, unavailable setup.
Check TypeScript/lint for touched files and existing bot regression suite. Do not send
a real announcement during verification. Deployment readiness is distinct from having
a configured bridge; report remaining connection configuration honestly.
