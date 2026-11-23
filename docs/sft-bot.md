# Shero Food Training (SFT) on WhatsApp

The $99 course's own WhatsApp journey, built beside the food ordering bot but
kept separate from it. It has its own leads, follow-ups, team hand-overs and
Zoho sync, and its own admin page at **`/admin/sft`**.

That page is **not in the dashboard menu yet**: open it by its address (same
login). To add it to the menu later, add one link to the sidebar in
`app/templates/admin/base.html` pointing at `url_for('admin_sft')`.

SFT is **off** until it is switched on in **Settings** on that page. While it
is off, nothing changes for anyone.

## The journey

| # | Stage | What the lead gets | Buttons |
|---|---|---|---|
| 1 | Lead enters | "May I know your name?", then the welcome with their name, the $99 fee, 10 videos · 10 quizzes · 3 cuisines | Start food business · Improve my skills (the old *Course details* button is gone) |
| 2 | Choose interest | How SFT helps that goal | View course details · Ask a question |
| 3 | Send brochure | The brochure PDF, then "Would you like to enroll now?" | **Yes, enroll me** · **Not right now** · **Ask a question** |
| 3a | Yes | The fee and a secure Stripe checkout button (Pay $99 to enroll) | |
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

Every message and button is on the page's **Journey** tab, and the wording is
in `app/sft/content.py`.

## When the team takes over

A lead who stops at a stage for too long becomes a task for the team (once per
stage), with a suggested message. The waits are set on the Settings tab:

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
  these options to the picklist).
* **Tags**: `SFT` on every SFT lead; one stage tag (`SFT New`, `SFT Contacted`,
  `SFT Interested`, `SFT Brochure Sent`, `SFT Questions`, `SFT Payment Pending`,
  `SFT Enrolled`, `SFT Nurture`, `SFT Lost`); `SFT Goal Business` / `SFT Goal
  Skills`; `SFT via <source>`; `SFT With Team` / `SFT Needs Call`.
* **Notes**: the timeline (stages, their question, payments, the team's actions).
* **Tasks**: the team's tasks above.

A number Zoho already has (a food customer) keeps its record, name and Lead
Source; it only gains the SFT tags and notes. Leads made in the Test chat
never go to Zoho.

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
together. A different brochure can be used by putting its link in Settings.

## Going live

1. Deploy. Migration `0025_sft` adds the two tables (`sft_leads`, `sft_events`).
2. On `/admin/sft` → **Test chat**, walk the journey (it sends nothing on
   WhatsApp). Use *Time travel* to see the follow-ups.
3. **Settings**: the course link (sent after paying), the test numbers, the
   team's WhatsApp numbers, and SFT's own channel id if it has one.
4. **Templates** → *Submit the missing ones to WhatsApp* (7 templates:
   `sft_followup_1..3`, `sft_payment_reminder`, `sft_course_check`,
   `sft_nurture`, `sft_team_alert`). Until Meta approves them, a follow-up
   goes as an ordinary message inside the 24-hour window and is skipped
   outside it.
5. **Settings** → *Prepare Zoho*.
6. Switch to **Test** and try it from a listed phone, then **Live**.

Payments use Shero US's Stripe account and its existing webhook
(`/webhooks/stripe`); SFT's checkouts are told apart by their metadata.
Without a Stripe key, and for test numbers in Test mode, the pay button opens
a test checkout that charges nobody.

## Code

`app/sft/` (see its `__init__.py`), the tables in `app/db/models/sft.py`,
the pages in `app/templates/admin/sft/` and `app/templates/order/sft.html`,
and the tests in `tests/sft/`.
