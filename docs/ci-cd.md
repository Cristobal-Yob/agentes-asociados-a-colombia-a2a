# Independent CI and explicitly gated cloud release

CI is read-only and hermetic. Agent and standalone UI delivery remain separate
workflows. Publishing educational source does not deploy services, alter IAM,
rotate keys or enable student access to a hosted environment.

## CI

`ci.yml` runs Python 3.12 checks on Linux/Windows for pull requests and main/codex
branches. It installs the lock, checks Ruff, runs deterministic tests and compares
regenerated contracts. The image job builds/checks agent images without cloud
credentials. Local process-based smoke tests are separate from provider proof.
`ci-demo-ui.yml` checks the standalone UI pins, browser syntax, offline boundaries
and allowlisted runtime package. Neither CI requires model, Gmail or cloud secrets.

Actions are pinned by commit. Pull requests do not obtain `id-token: write`, run
`pull_request_target` or inherit a deployment session. Do not add credentials to a
public PR merely to make CI pass. Tests mocking an accepted provider response do
not show real email delivery, Google login or Calendar creation.

## Manual deployment gate

Both `cd-cloud-run.yml` and `cd-demo-ui.yml` are `workflow_dispatch` only. They also
require **`PUBLIC_SOURCE_DEPLOY_ENABLED=true`**, a new explicit repository/protected
environment opt-in, before any cloud-token-bearing job runs. It is not configured
by source publication. Pushes cannot trigger a deployment or request cloud tokens.
Do not enable this flag until reviewing a specific own-account deployment scope.

For an authorized deployment, independently provision the target resources and
protected GitHub environments with required reviewers and main-only access.
Use narrowly repository/environment/ref-restricted WIF and separate deploy/runtime
service accounts. No service-account JSON keys belong in source or Actions secrets.

| Agent release configuration | Required operator choice |
|---|---|
| `EXPECTED_GITHUB_REPOSITORY` | Exact approved owner/repository, matching runtime GitHub metadata |
| `RELEASE_GCP_PROJECT` | Explicit own project, matching the checked release target |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Own repository-restricted provider |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Separate account in that project, not a runtime account |
| `GMAIL_EXPECTED_EMAIL` | Independently authorized own mailbox |
| Existing services/resources | Own agent services, secret versions, registry and durable ledger described by the release preflight |

The agent release implementation validates existing services, IAM enforcement,
resource limits, runtime identity, canonical URL, secrets, shared ledger and fixed
traffic before building. It stages an explicit runtime allowlist, publishes by
digest and prepares revisions **without traffic**. Missing resources stop the
release; this workflow is not an infrastructure bootstrap or public IAM grant.
A local planning script prints commands only and is not authorization to run them.

## Standalone UI release

The UI uses its own protected `classroom-ui-release` environment and `COLOMBIA_UI_*`
variables. Supply its explicit own project, allowed Google owner, origins, fixed
hub return, expected mail sender, dedicated Firestore database, distinct runtime
and deploy identities, WIF, registry, reviewed Python image digest and numeric
secret versions. The selected approved SHA must equal the actual main commit.
Configuration fails before cloud authentication when inputs are missing or unsafe.
The UI must not reuse the Gmail Desktop OAuth client for browser login.

The workflow requires an existing UI service, captures its configuration, builds
only the independent allowlisted package and creates a digest-pinned revision
without traffic. It does not create OAuth clients, databases, IAM bindings or
public invocation. Real OAuth, anonymous denial, owner acceptance, private agent
calls and any separately authorized send require independent acceptance. Do not
promote traffic or advertise a live UI based only on source tests.

## Rollback

Keep exact source SHA, image digest, previous explicit traffic map and reviewed
configuration privately. Do not delete state, ledgers or active signing keys during
a release rollback. Public history/source sanitization is not a cloud rollback and
must not be deployed over existing operational services without a separate review.
