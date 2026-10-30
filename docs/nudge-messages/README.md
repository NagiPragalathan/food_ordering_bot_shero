# SMO follow-up messages

The 54 messages from section 10 of the SMO developer handoff
(`docs/Shero_SMO_Only_Developer_Handoff.pdf`), M1-01 to M6-09, word for word
with their emojis, ready to import on the Nudges page:

**Nudges › Messages › Import**, choose either file, check, then Import.

| File | For |
|---|---|
| `SMO-54-follow-up-messages.xlsx` | Editing in Excel before importing |
| `SMO-54-follow-up-messages.json` | The same, as JSON |

Each message is in the SMO pool, under its month, active, with no reply-group
limit, and with its WhatsApp template name (`smo_m1_01`...). All 54 templates
were submitted to Meta through Gallabox on 30 Oct 2026 (category Marketing,
the same text, no variables or buttons). Until a template is approved, its
message only goes to customers who messaged the bot in the last 24 hours.
Approved templates go to every number, US included (Meta currently reports
Marketing templates to US numbers as not delivered). Change a message's text
and its template must be submitted again under a new name.
Both files carry these notes (the JSON's `read_me`, the Excel "Status and
limits" sheet); the import ignores them.

An occasion message (with From and Until dates) can also have an **Order by**
cutoff and a **Kitchens** service area; both are columns in the file and
fields on the page.

The handoff says the dish names are examples, to be checked against the
live menu before activation. Edit them in the sheet or on the page.
Importing the file again updates the messages by ID and changes nothing
else. How journeys use the messages: `docs/nudges.md`, "Follow-up journeys".
