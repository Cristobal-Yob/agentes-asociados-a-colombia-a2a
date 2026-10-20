# Optional Agenda and your own Calendar authorization

Agenda is a third independent FastAPI/LangGraph service. It is optional for learning
Itinerary and Email. Default/offline execution does not access Calendar or create
events. Calendar live authorization and acceptance are not implied by this source.

## Separate own-account consent

Set `GMAIL_EXPECTED_EMAIL` and `GMAIL_OAUTH_PROJECT` to your selected own account and
Desktop-client project before starting Python. The shared identity verifier checks
those bindings, but Calendar uses its own grant, scopes, signing key and ledger;
it does not accept the Gmail-send grant. Never distribute an instructor's files.

```sh
python -B -m scripts.authorize_calendar --client /private/desktop-client.json --output /private/calendar-user.json --signing-key-output /private/agenda-key.txt
```

Run only after separately approving consent. Required scopes are `openid`, `email`
and `calendar.events.owned`. Configure `CALENDAR_CREDENTIALS_FILE`, a stable
`AGENDA_APPROVAL_SIGNING_KEY` and private `CALENDAR_LEDGER_PATH` outside source.
Live model use also needs its own explicit `MODEL_MODE=vertex` and
`VERTEX_PROJECT_ID`. This command neither reads nor sends Gmail.

## Planning and effects

Talk to Agenda directly via its own `/api/chat` or A2A interface. Supply date,
IANA timezone, start time and duration; the agent asks for missing data, checks
Calendar conflicts and returns a signed proposal. Review it before the explicit
confirmation that creates events. Source data or model suggestions alone cannot
confirm them. Current accounts are selected by server configuration, not a prompt.

The service rechecks conflicts, reserves the plan durably and creates deterministic
event IDs. It verifies accepted events independently. A partial or unknown batch
is not a success and is not automatically retried. Calendar changes between
conflict check and creation are not atomic; exactly-once scheduling is not promised.

If Itinerary delegates to Agenda, Agenda owns the requested final email summary;
Itinerary does not send a duplicate summary. Email remains responsible for its own
intent, approval, provider and receipt checks. No invitations or bookings are made.

For safe study, run the `test_agenda*` Python tests. They inject calendar/model
boundaries and do not prove real authorization or event creation.
