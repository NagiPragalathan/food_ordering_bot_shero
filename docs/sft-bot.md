# Shero Food Training (SFT) on WhatsApp

The $99 course's own WhatsApp journey, built beside the food ordering bot but
kept separate from it. It has its own leads, follow-ups, team hand-overs and
Zoho sync, and its own admin page at **`/admin/sft`**.

That page is **not in the dashboard menu yet**: open it by its address (same
login; signing in from the address brings you back to it). It has its own
layout with only SFT's sections. To add it to the dashboard's menu later, add
one link to the sidebar in `app/templates/admin/base.html` pointing at
`url_for('admin_sft')`.

SFT is **off** until it is switched on in **Bot replies** on that page. While
it is off, nothing changes for anyone.

## The admin page

| Section | What it shows |
|---|---|
| Overview | KPIs, a daily chart of leads and enrollments, the funnel (click a stage), sources, goals, who needs the team |
| Leads | every lead as a list or a **board by stage**, each with its flow and what the bot does next; search, source, stage; CSV |
| Customers | everyone who paid, with the money on top: paying customers, revenue, link-to-paid, discounts, refunds; each customer's payment (Stripe, **receipt**) and course access (opened, not confirmed yet, needs the team); who is waiting to pay; revenue by day; CSV |
| Nudges | **Rules** (on/off, send hours, each follow-up's wait and switch, the payment reminder, Maybe later check-ins, the course check, when the team takes over), **Leads by flow**, **Messages sent**, **WhatsApp templates** |
| Offers | codes and launch prices (below) |
| Bot replies | who SFT answers (Off / Test / Live, test numbers), what starts it (keywords, source codes, ad ids, own number), **Check a message**, and **every message the bot says, with its buttons, editable** |
| Test chat | the journey without WhatsApp, with time travel for the nudges |
| Settings | the course (fee, links), Stripe's status, the team's numbers, Zoho |

A lead's page has **Delete this lead and start over**: the lead, its
timeline and receipt go, its follow-ups stop, and the bot forgets the number
(its next "SFT" starts with the name). Zoho keeps its record; a real Stripe
payment needs a tick to confirm. Handy for testing from your own phone.

Payments and Journey used to be tabs of their own; they are now part of
Customers and Bot replies (old links open there).

Overview, Leads, Customers and the Nudges' lists take a **date
range**: Today, Yesterday, Last 7 / 30 / 90 days, This month, Last month,
All time, or a custom From-To (in SFT's timezone). The range stays as you move
between them.

## The journey

| # | Stage | What the lead gets | Buttons |
|---|---|---|---|
| 1 | Lead enters | "May I know your name?", then the welcome with their name, the $99 fee, 10 videos · 10 quizzes · 3 cuisines | Start food business · Improve my skills (the old *Course details* button is gone) |
| 2 | Choose interest | How SFT helps that goal | View course details · Ask a question |
| 3 | Send brochure | The brochure PDF, then "Would you like to enroll now?" | **Yes, enroll me** · **Not right now** · **Ask a question** |
| 3a | Yes | The fee and a Pay button. It opens the **enroll page**: the course (5-day plan, topics, how certification works), the order summary with any offer, then Stripe's secure checkout | |
| 3b | Not right now | "No problem… reply SFT any time", then the automated follow-ups | |
| 3c | Ask a question | "Please type your question". When they do: *"We're transferring you to a member of our team, who will reply to you here shortly."* **The bot then sends this lead nothing (no replies, no follow-ups) until someone on the team hands the chat back** on the SFT page | |
| 4 | Questions (before the brochure) | A list: What will I learn? · Do I get a certificate? · Kitchen Partner pathway · How much does it cost? · Ask my own question (goes to the team) | View course details · Enroll now · Another question |
| 5 | Payment pending | The pay link again after an hour if unpaid | |
| 6 | Paid | Confirmation and the course link; a day later "have you been able to open your course?" | Yes, I'm in · I need help (goes to the team) |
| 7 | Follow-ups | After 24 h, 48-72 h and 5-7 days of quiet (WhatsApp templates) | Enroll now · Ask a question / Maybe later · Not interested |
| 8 | Maybe later | "When would be a good time?" A check-in that day, then 30 days later | In a week · In a month · I'll message you |
| 9 | Not interested | Stopped; "what's the main reason?" | Price · Timing · Something else |

Free text a lead types where the bot expected a tap is read sensibly: "yes"
after the brochure enrolls them, a question ("Is it in English?") goes to the
team, and "ok" or "hi" gets the options again. STOP opts out.

Every message and button is on the page's **Journey** tab. The wording is
edited on **Bot replies** (each message lists the placeholders it can use,
such as `{first}` and `{fee}`; "Default" puts the original back); the
defaults are in `app/sft/content.py`. The buttons keep their words, since the
bot reads the lead's taps by them.

## Offers

* **Code**: the lead sends it on WhatsApp ("SFT20", or "SFT SFT20" from an
  ad). The bot confirms the new fee and carries on where they were; a code
  starts the journey even on Shero US's shared number.
* **Automatic** (no code): covers everyone, or the sources ticked, between
  its dates, such as a launch price.

Each is a percent off, an amount off or a fixed price, with optional dates
and a limit on enrollments. A lead pays the lowest price that applies (never
below Stripe's $0.50); it shows in the welcome, the brochure, the FAQ, the
Pay button and Stripe's checkout, and an open checkout at an old price is
replaced. The approved follow-up templates keep saying the full fee.

## When the team takes over

A lead who stops at a stage for too long becomes a task for the team (once per
stage), with a suggested message. The waits are set on **Nudges → Rules**:

| Stage | Task after | Suggested message |
|---|---|---|
| Lead enters (no option chosen) | 2 hours | "Hi {name}, saw your enquiry about SFT. What are you looking to achieve…?" |
| Chose a goal, no next step | 4 hours | "You were interested in {goal}. Let me share how SFT helps you with that." |
| Brochure sent, no answer | 24 hours | "Did you get a chance to see the SFT brochure? Do you have any questions?" |
| Question unresolved | 12 hours | "I can help with your question about the curriculum, certificate, partner pathway or fee." |
| Payment link sent, not paid | 12 hours, **high priority** | "I noticed your enrollment is not yet complete. Any help needed with the payment?" |
| Paid, course not opened | 24 hours after the check | "Congratulations again! Have you been able to access your course?" |
| No answer to all follow-ups | 24 hours after the last | "Just checking if you're still interested in SFT or need more information." |
| Asked a question / needs help opening the course | at once, **high priority** | Answer in Gallabox, then hand back |

Tasks show on the **Overview** tab ("For the team now") and on each lead's
page. In Zoho each one is also a Task on the lead (due today). On the lead's
page the team closes it: *Answered: hand back to the bot*, *Carry on with the
journey*, *Follow up in a week / month*, *Enrolled outside the bot* or
*Close as lost*.

## Zoho CRM (Shero US's)

Synced every minute by a job, never inside the chat.

* **Lead Source**: `SFT - WhatsApp`, `SFT - Meta Ad`, `SFT - Instagram`,
  `SFT - Website`… (press **Prepare Zoho** once on the Settings tab to add
  these options, and the Lead Statuses below, to the picklists).
* **Lead Status** (the funnel, on the bot's own SFT Leads): `SFT - New`,
  `SFT - Contacted`, `SFT - Interested`, `SFT - Brochure Sent`,
  `SFT - Questions`, `SFT - Payment Pending`, `SFT - Enrolled`,
  `SFT - Nurture`, `SFT - Lost`. In Zoho: filter Leads by Lead Source starts
  with "SFT", group by Lead Status.
* **Tags**, where the Zoho plan has them (Zoho **Free does not**: its first
  "NOT_SUPPORTED_FEATURE" switches tags off and the sync carries on with Lead
  Status and notes; Prepare Zoho tries tags again, e.g. after an upgrade): `SFT` on every SFT lead; one stage tag (`SFT New`, `SFT Contacted`,
  `SFT Interested`, `SFT Brochure Sent`, `SFT Questions`, `SFT Payment Pending`,
  `SFT Enrolled`, `SFT Nurture`, `SFT Lost`); `SFT Goal Business` / `SFT Goal
  Skills`; `SFT via <source>`; `SFT With Team` / `SFT Needs Call`.
* **Notes**: the timeline (stages, their question, payments, the team's actions).
* **Tasks**: the team's tasks above.

A number Zoho already has (a food customer) keeps its record, name and Lead
Source and Lead Status (the ordering bot's); it only gains the SFT notes
(and tags, where the plan has them). Leads made in the Test chat never go to
Zoho. Shero US's Zoho is the Free edition: no tags, and the 10 custom fields
per module are used by the ordering bot, which is why SFT uses Lead Status.

## Where leads come from (for the analytics)

* **Meta Ad**: a Click-to-WhatsApp ad. Its chats start the journey when the ad
  id is listed in Settings, or when the ad's headline mentions SFT.
* **Source codes**: "SFT IG", "SFT web", "SFT qr"… (or a wa.me link with that
  text) file the lead under Instagram, Website, QR code… Edit the codes in
  Settings.
* Otherwise **WhatsApp**.

The Overview tab shows the numbers by source, the funnel, and the KPIs:
lead-to-contact, interest captured, brochure engagement, payment link opened,
link to paid, overall conversion, course access, recovery after a follow-up,
and the team's response time.

## From the ordering bot's welcome menu

The US dashboard's **Bot replies → Welcome menu** can have a button that goes
to **Shero Food Training (SFT bot)** (e.g. "Shero Food Training"; four or more
options go as a list). Its tap is SFT's whatever the label says, and starts
the journey; the lead's source is WhatsApp, detail "welcome menu". The food
menu never goes to someone SFT is talking with (24 hours from SFT's last
message either way, or while the team has the chat), and SFT never takes the
ordering bot's messages. If the old greeting still arrives mid-SFT, it is
Gallabox's own **Main Card** (any keyword): turn it off in Gallabox.

## Which WhatsApp number

* **Shero US's number** (the default): a chat becomes SFT's only when it asks
  for it (the keyword SFT, an SFT button, an SFT ad) or is part-way through
  the journey. Everything else stays with the ordering bot, including the
  food bot's own trigger keywords mid-journey.
* **SFT's own number**: put its Gallabox channel id in Settings. Every message
  on that number is then SFT's, and Shero US's number is left alone.

## The brochure

`app/static/sft/shero-food-training-brochure.pdf` (5 pages), served at
`<PUBLIC_BASE_URL>/static/sft/shero-food-training-brochure.pdf` and sent by the
bot at stage 3. It is printed from `docs/sft-brochure/brochure.html`:

    chrome --headless=new --no-pdf-header-footer --print-to-pdf=app/static/sft/shero-food-training-brochure.pdf docs/sft-brochure/brochure.html

Its facts match the bot's FAQ answers (`app/sft/content.py`); change both
together.

**Replace the PDF** on the Settings tab (PDF only, up to 16 MB, Gallabox's
limit): it is kept in the data volume (`data/media/sft/<hash>/Shero-Food-
Training-SFT-Brochure.pdf`, served under `/media/`), sent from then on, and
**Use the original** goes back. A link to a brochure elsewhere can be set
instead (it must open the PDF itself).

Gallabox's document message is `{"path": <link>, "mimeType":
"application/pdf", "caption": ...}`. Meta's own `link`/`filename` shape is
answered ACCEPTED by Gallabox and then dropped, so the file never arrived.

## The enroll page and the receipt

`/sft/enrol/<token>` shows the course and the order summary (fee, offer,
total) with Pay; Pay opens Stripe's checkout (`/sft/enrol/<token>/pay`),
whose Back link returns to the page. In Test mode (or without a Stripe key)
Pay marks the lead paid with nobody charged, and the page says so.

Once paid, the page shows "You're enrolled" with the receipt number, and the
bot sends, after the payment confirmation, the **enrollment receipt PDF**
(`SFT-Enrollment-Receipt-SFT-<date>-<id>.pdf`: billed to, receipt details,
course, offer, total paid, next steps; a test payment is marked as one). It
is drawn at payment and kept under `data/media/sft/receipts/`, so the link
works the moment WhatsApp fetches it; `/sft/receipt/<token>/<file>` serves it
(and draws it again if it is missing). The lead's page in the admin links it.

## The nudges

On **Nudges → Rules**: all of them on or off (off: nothing is sent, they
wait), the hours they may go out, and each one with its wait and its own
switch (a follow-up switched off is skipped and the next follows on time).
Two follow-ups are at least a day apart, so a lead quiet for a long time is
not sent them back to back. **Leads by flow** shows every lead in its flow
(in the journey, follow-ups running, follow-ups finished, waiting to pay,
maybe later, paid: course check, with the team, finished, stopped) with what
the bot sends next and when.

## Going live

1. Deploy. Migrations `0025_sft` (`sft_leads`, `sft_events`) and
   `0026_sft_offers` (`sft_offers`, and a lead's offer and last nudge).
2. On `/admin/sft` → **Test chat**, walk the journey (it sends nothing on
   WhatsApp). Use *Time travel* to see the follow-ups.
3. **Settings**: the course link (sent after paying) and the team's WhatsApp
   numbers. **Bot replies**: the test numbers, and SFT's own channel id if it
   has one.
4. **Nudges → WhatsApp templates** → *Submit the missing ones to WhatsApp* (9 templates:
   `sft_followup_1..3`, `sft_payment_reminder`, `sft_course_check`,
   `sft_nurture`, `sft_team_alert`, `sft_enrolled`, `sft_payment_failed`).
   Until Meta approves them, a follow-up goes as an ordinary message inside
   the 24-hour window and is skipped outside it.
   * `sft_enrolled`: the payment confirmation when they pay after the 24-hour
     window (amount, receipt number); its **Get my course** brings the course
     link and the receipt PDF. Not approved yet: the team gets a task.
   * `sft_payment_failed`: a failed payment after the window; **Complete
     enrollment** sends the checkout link again.
   The other `sft_*` templates in Gallabox (`sft_lead_welcome`,
   `sft_create_interest`, `sft_explain_outcome`, `sft_handle_decision`,
   `sft_not_interested`) belong to the Gallabox SFT flow, not this bot.
5. **Settings** → *Prepare Zoho*.
6. On **Bot replies** switch to **Test** and try it from a listed phone, then **Live**.

Payments use Shero US's Stripe account and its existing webhook
(`/webhooks/stripe`); SFT's checkouts are told apart by their metadata.
Without a Stripe key, and for test numbers in Test mode, the pay button opens
a test checkout that charges nobody.

## Code

`app/sft/` (see its `__init__.py`), the tables in `app/db/models/sft.py`,
the pages in `app/templates/admin/sft/` and `app/templates/order/sft.html`,
and the tests in `tests/sft/`.
