# Student-facing source and privacy notes

This repository is prepared for students to study and reproduce independent
Itinerary and Email agents, A2A, bounded MCP retrieval and durable state. The owner
has not selected an open-source license; license selection remains pending and no
root LICENSE is included. Making source
visible does not itself select a license or grant use of somebody else's services.

## Learning and access boundary

Start with the README learning path and deterministic tests. Public source does
not enroll students in hosted agents, grant production access or authorize mail.
Never distribute institutional keys, instructor credentials, OAuth grants or shared
sessions. Any live exercise requires own independently authorized accounts,
configuration, bounded usage and a current explicit send request.

The source is not a new remote repository or a hosted service. The same GitHub
repository identity remains visible; GitHub actor identity is public metadata,
not an anonymity guarantee. Personal deployment addresses, origins, projects and
account paths are replaced by reserved examples and explicit configuration.
Private replacement maps/backups are not publication files.

## Implemented versus not established here

| Source implementation | Not proved by source publication |
|---|---|
| Independent FastAPI/LangGraph Itinerary and Email | A hosted classroom entitlement |
| Native model decisions and typed REST/A2A SDK | Fresh live verification of every provider path |
| SQLite/dedicated Firestore state and versions | Exactly-once external effects |
| Draft/send separation and effect ledgers | Inbox delivery or reading |
| Optional Agenda and Google Calendar adapter | Live Calendar authorization or event acceptance |
| Structured owner-configured Web OAuth UI | Deployed UI, real OAuth acceptance or IAP |
| Read-only CI and separately gated manual CD | Deployment authorization or cloud resource creation |

## Safe configuration

Runtime and image defaults are fixture/mock/real-mail-off. API key enforcement,
signed previews, exact sessions/versions and duplicate suppression remain active.
Configure a live model project explicitly before ADC discovery. Configure the
expected Gmail mailbox/project before process import; grants and UserInfo must
match them. The UI requires an explicit owner email and fixed upstream/hub URLs,
not browser-supplied destinations. No instructor environment is a default.

Cloud release workflows are manual, protected and additionally require a new
`PUBLIC_SOURCE_DEPLOY_ENABLED=true` opt-in. Source pushes and pull requests run
hermetic checks only, without cloud tokens, Gmail, models or deployment. The
original operational environment and its keys/IAM/OAuth are not changed by this
sanitized source. Do not deploy this teaching variant onto an existing service
without its own separate migration review and authorization.

## Privacy review limitations

The owner handles sanitizing the original repository's relevant branches/history
before changing visibility. A current-file scan alone cannot remove old commits,
Actions logs/artifacts, releases, caches or copies retained by a hosting provider.
Old object reachability must be checked after history publication; residual
personal-data availability remains a release blocker. No personal data is placed
into this public explanation to describe its removal.

A bounded pattern scan is not a secret-freedom certification. If a real credential
is discovered, stop and arrange revocation/rotation and authorized cleanup; editing
source is not revocation. Do not rewrite history, delete artifacts or rotate keys
without the owner's explicit scope. The private transaction report records exact
review coverage and evidence separately from educational source.

## Acceptance and rollback

Run the README offline tests and `tests/test_source_privacy.py`. They check
reserved email examples, fail-closed own configuration, offline defaults and
manual cloud gates. Tests mock providers; they are not evidence of paid calls,
OAuth login, real sending or delivery. Publication retains substantive agent,
conversation, recovery and approval tests, not toy replacements.

The owner retains a private original-history backup and untouched operational
authoring tree. This public teaching-source variant is not a deployment or traffic
change. Rollback/publication decisions use those private records; no secrets,
original-personal replacement map or operational database belongs in source.
