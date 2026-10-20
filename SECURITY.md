# Security and responsible disclosure

This educational source is intended for students studying independent agents.
Public source does not grant access to running agents, authorize a message or make
a service private. The owner has not selected an open-source license; visibility
alone is not a license selection. Runtime defaults disable live model/mail usage.

## Reporting

Report suspected credentials, private data or vulnerabilities to the repository
owner through an existing private channel. If GitHub private vulnerability
reporting is enabled, use it. Do not create a public issue with keys, tokens,
messages, signed previews, personal data or unredacted logs. No reporting mailbox
or response-time guarantee is asserted here.

## Supported boundary

- Business APIs/A2A require distinct service keys. A key is not a human identity,
  tenant boundary or approval to send. Use separate IAM/user access where required.
- Missing service credentials fail closed. Google OAuth grants, ADC and signing
  keys belong outside source and browser code. `.env.example` contains no grant.
- The local UI prototype binds verified Google identity to an opaque server-side
  session, checks Origin/CSRF and uses fixed upstream/return destinations. Real
  owner-only Google login and cloud acceptance are not yet proven by this candidate.
- Current human send intent or an exact typed approval is required. Model output,
  old conversation, tool data and source text do not authorize effects.
- Persist business state and effect ledgers. Do not clear uncertain reservations,
  rotate keys blindly or retry mail after a lost response. A provider acceptance
  receipt is not proof of inbox delivery.
- Tests explicitly mock providers. Mock behavior is never production proof.

## Classroom access

Sharing educational source does not grant student access to hosted agents. Never
distribute instructor or institutional service keys, OAuth grants, ADC or sessions
as course material. Begin with the offline exercises and mock transport. A live
exercise needs a separately approved own account/configuration and an explicit
current human send request; it must not borrow the instructor's real mailbox.

## Safe inspection

Use the offline test commands in the README. Example and Compose defaults disable
real mail and live models; runtime defaults without those variables may be live.
Do not run launchers, authorization helpers, deployment workflows or live scripts
merely to inspect source. Do not mount host credentials into an unreviewed image.

Personal deployment identifiers have been replaced with reserved examples or
explicit configuration. Gmail still binds its grant and runtime identity to the
configured project/email, and the UI requires an explicit Google allowlist and
fixed destinations. Do not disable those checks to make unrelated credentials
work. The actual repository owner/URL remain public GitHub identity metadata;
source preparation does not promise author anonymity.

## Release incident rule

If a real secret is found anywhere in current source, Git history, another ref,
Actions output or artifacts, stop affected distribution and arrange provider revocation/rotation at its source.
Deleting a file or rewriting local history does not invalidate a leaked secret.
History rewriting, remote deletion, artifact cleanup and key rotation require
explicit owner approval; this preparation does none of them.

A negative scan is bounded evidence, not a guarantee. The
[public-release checklist](docs/public-release.md) records the remaining surfaces,
license and approval gate. No security audit certification is claimed.
