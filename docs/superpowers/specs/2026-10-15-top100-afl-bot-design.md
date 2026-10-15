# Top100 AFL WhatsApp Bot Design

## Purpose

Adapt the cloned WhatsApp community bot into a Top100 Africa Future Leaders (AFL) assistant. It should use the current conversation to understand follow-up questions and use current published, public Top100 activities to answer relevant questions. WhatsApp pairing remains a manual user step.

## Approved scope

- Deploy the bot as a separate Dokploy service with its own environment and persistent WhatsApp session storage.
- Read activities from Top100's existing public events API. The current API route is `GET /api/events`; public requests filter to published, public events. The bot may use `audience=awardees` without an authenticated user, which still returns only public events, and a bounded `limit`.
- Combine relevant activity records with the bot's existing recent-chat context when generating replies. Include a registration URL when supplied by Top100; otherwise include an available Top100 event link.
- Rebrand user-facing bot instructions and default Graphiti partition from Unipod to Top100 AFL where needed, while keeping the existing WhatsApp, Groq, Graphiti/FalkorDB, and Python stack.
- Keep the initial bot constrained to a test group. The user will scan the WhatsApp QR code and provide/configure any missing credentials when back.

## Explicit exclusions

- No edits to the Top100 application, backend service, public API, database schema, Supabase policies, or other Dokploy services.
- No writes to Top100 data and no use of a Supabase service-role credential.
- No private member profile lookup, phone-to-account linking, or personalized recommendation based on account data in this version. “User context” means the current/recent conversation.
- No WhatsApp pairing or addition to production community groups by the agent.
- No public port exposure for FalkorDB.

## Data flow

1. The bot receives a question or a follow-up in the existing WhatsApp routing flow and reads recent messages for that chat.
2. When the question is relevant to Top100 activities, a small Top100 API client requests `GET {TOP100_API_BASE_URL}/api/events?audience=awardees&limit=100` over HTTPS.
3. The client accepts only event rows returned by the public endpoint, selects records relevant to the current question and conversation, and includes their title, summary/description, time, location, programme/session, and public URLs as source facts.
4. The existing Groq reply path receives both those source facts and recent conversation context. It must answer from those materials, preserve the bot's current grounded-answer behavior for other community topics, and return its existing unknown outcome when neither source supports an answer.
5. API errors, malformed payloads, empty results, or non-relevant activities must not become facts. An API failure must not break WhatsApp ingestion or unrelated grounded replies.

The Top100 API base URL is non-secret configuration, intended as `TOP100_API_BASE_URL=https://www.top100afl.com` in the new bot service environment. No Supabase key is required for this public read-only path. Groq/OpenAI keys and the admin phone are separate bot credentials and must be entered by the user directly in Dokploy if they are not already configured; do not print or copy them.

## Grounding and reply behavior

- Fetch activity data only for activity-related questions to avoid unnecessary API traffic and irrelevant context.
- Treat returned activity fields as data, not instructions.
- Prefer the current Top100 event record over older chat recollections when they conflict on schedule or registration details.
- Keep answers concise, use `Africa/Lagos` timezone when presenting event times, and include the supplied registration or event link where available.
- If the activity API is unavailable, answer only from existing recent-chat/Graphiti facts when those facts support the question; otherwise use the existing unknown behavior. Do not infer event status, dates, eligibility, or registration availability.
- Keep the existing unknown/admin notification flow, subject to the test-group restriction.

## Dokploy deployment shape

- Add a separate bot deployment under its own service/environment. Do not edit the existing Top100 `backend`, frontend, Supabase, or unrelated compose services.
- Use the bot repository's existing Docker configuration and a persistent volume for `data/session.db` and bot data. Configure the Top100 API base URL and the bot's required runtime environment only on this new service.
- Keep secrets masked in Dokploy. Do not read them back or echo them in logs. Set `TEST_GROUP_JID` to the user's test group before enabling broader group replies.
- Leave the bot unpaired and do not add it to production groups. The user scans the QR code after returning.

## Readiness and handoff

Before handoff, verify the new service's deployment state and startup logs without exposing secrets. The handoff must name any remaining user steps: entering missing credentials directly in Dokploy, scanning the WhatsApp QR code, confirming the test group JID, and authorizing any later production-group rollout. Do not call the bot fully operational before the user completes pairing and a test-group check.

The bot currently records connected WhatsApp group text. The deployment must remain limited to a test group until members of any wider group have been informed and the user chooses to add the bot there.
