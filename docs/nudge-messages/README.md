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
limit, and with a suggested WhatsApp template name (`smo_m1_01`...). Create
each template in Gallabox (category Marketing) with the same text and that
name. Until a template is approved, its message only goes to customers who
messaged the bot in the last 24 hours.

An occasion message (with From and Until dates) can also have an **Order by**
cutoff and a **Kitchens** service area; both are columns in the file and
fields on the page.

The handoff says the dish names are examples, to be checked against the
live menu before activation. Edit them in the sheet or on the page.
Importing the file again updates the messages by ID and changes nothing
else. How journeys use the messages: `docs/nudges.md`, "Follow-up journeys".
