# Authorize your own Gmail account explicitly

Offline tests and default runtime use mock transport. They do not need a Google
account or send mail. This optional adapter requires your own approved Desktop
OAuth client, selected mailbox and private local storage. It never borrows the
instructor's credentials and requests no mailbox-read permission.

## Required live configuration

Set these before starting the Python process; the expected identity is bound at
module import and is not accepted from a user message:

- `GMAIL_EXPECTED_EMAIL`: your exact approved mailbox, not a reserved example.
- `GMAIL_OAUTH_PROJECT`: the project owning your Desktop OAuth client.
- `MODEL_MODE=vertex` and `VERTEX_PROJECT_ID`: your separately authorized model scope.
- `EMAIL_TRANSPORT=gmail`, `EMAIL_SENDER` matching that mailbox, and
  `ENABLE_REAL_EMAIL=true` only for an approved live exercise.
- `GMAIL_CREDENTIALS_FILE`: private absolute grant path outside the checkout.
- `APPROVAL_SIGNING_KEY`: own stable secret, 32 to 4096 bytes, never source-controlled.
- A private persistent effect ledger (`GMAIL_LEDGER=sqlite` locally or dedicated
  approved `gcs` for replicated runtime), retained across restarts.

Do not paste values into source or `.env.example`. Python does not load an `.env`
file automatically. Model ADC and Desktop Gmail authorization are independent.
The source contains no grant, service key or account enrollment.

## Consent helper

First prepare your own Desktop OAuth application, Gmail API access and any required
test-user/consent configuration under that project. This is separate operator work;
no helper creates a project or bypasses Google's access decisions.

```sh
python -B -m scripts.authorize_gmail --client /private/desktop-client.json --output /private/authorized-user.json --signing-key-output /private/approval-key.txt
```

This is an explicit consent action, not an offline quickstart. The helper opens a
loopback callback and verifies state, PKCE, nonce, signature, issuer, audience,
expiry, verified email and subject before saving a protected new file. Scopes are
`openid`, `email` and `gmail.send` only. It does not read or send an email. A timeout
or denied account is not success; do not reuse a callback code.

The helper refuses missing configured identity before starting consent. Existing
files are not overwritten. Reauthorization and rotation need their own scope;
preserve signing keys and ledgers while approvals may still exist.

## Runtime and send evidence

The Gmail transport refreshes narrow OAuth and checks UserInfo against the selected
mailbox and stored subject before reserving an effect. Changing a chat message or
`EMAIL_SENDER` alone cannot select a different identity. A typed send still needs
an exact signed preview, session, version and approval. A native chat send needs
current explicit human intent; drafting alone never sends.

A durable reservation precedes the provider POST. Accepted receipts reproduce
without another automatic attempt; reserved/unknown outcomes do not retry. A crash
can leave uncertainty even before an effect occurred. Do not erase state or create
new IDs to bypass it. `provider_accepted` does not prove arrival or reading.

On Cloud Run, shared persistent state and least-privilege identities must be
provisioned separately; do not use ephemeral SQLite for replicated effects.
No live mail, OAuth login or cloud change is performed by source publication.
