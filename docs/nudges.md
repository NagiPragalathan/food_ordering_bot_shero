# Nudges

**Nudges** in the admin sidebar sends WhatsApp messages to customers who
stopped part-way through ordering, and follows up with those who answer
**Later** or **No**, or don't answer at all. It works like KOB CRM's
Targeting Rules, except that every rule here can be changed. Code:
`app/services/nudges/`, `app/admin/routes/nudges.py`.

## The top bar

* **Nudges on**: nothing is sent while this is off. It starts off.
* **Send between ... and ...**, in the **time zone**: messages only go in
  these hours. One that falls due at night waits for the morning.
* **Write to Zoho**: put each customer's bucket and messages on their Zoho
  Lead or Contact (see below).

## Messages

Every message is a WhatsApp template with three buttons: **Order Food**,
**Later** and **No**. On the **Messages** tab, give it a name and the
text; `{name}` becomes the customer's first name. **Save and send for
approval** submits it to Meta through Gallabox. It shows **Waiting for
approval** until Meta approves it, usually within minutes, sometimes a day.
A rule only sends an approved message.

Gallabox can't edit or delete a template, so a message's text can't change
once it's saved: add a new one and choose it instead. **Remove** takes an
unused message off the list. The template stays in Gallabox until it's
deleted there.

## Targeting Rules

One row per stage of the ordering funnel (New Enquiry, Cart Created,
Payment Abandoned...; Converted is never nudged):

* **N hours in stage**: once a customer has been in the stage this long,
  they get the chosen message. This happens once each time they enter the
  stage.
* **N hours after that message**: if they are still in the stage this long
  after the message, they move to **No response**. Leave it empty to keep
  them in the stage.
* **On**: the rule only runs while ticked, and needs a message.

Defaults: 3 hours for Cart Created, Outlet Selected, Slot Selected, Payment
Abandoned and Payment Failed, and 24 hours for the rest. Every rule starts
off.

Customers who reached a stage more than 7 days before its wait ended are
left alone, so turning a rule on doesn't message everyone who stopped there
months ago.

## Retargeting Sequence

Three buckets, each with its own messages:

| Bucket | How a customer gets there | Default |
|---|---|---|
| **No response** | Didn't answer a stage message in time | 24 hours in, then every day x 2, every 10 days x 3, then every 30 days |
| **Later** | Tapped **Later** | 7 days in, then every 10 days x 2, then every 30 days |
| **No** | Tapped **No** | One message 30 days in |

For each bucket:

* **First message**: when it goes after they enter the bucket, and which message.
* **Follow-ups**: up to three phases of "every N days x times". Leave
  "times" empty on the last phase to repeat it. The follow-ups send the
  chosen follow-up message, or the first message again.
* **Dead Lead**: after so many days in the bucket, or after so many
  messages. The defaults are 180 days, and one message for No.
* **Reply when they tap Later / No**: what the bot says straight away.

The card draws the timeline it will follow: *24 hours in, +1 day, +1 day,
+10 days...*

## What the customer can do

* **Order Food**: starts an order as usual. It also takes them out of any bucket.
* **Later** / **No**: moves them to that bucket and sends its reply. Typing
  "later" or "no" works too, but only within 14 days of a nudge and not
  while they're ordering.
* **STOP**: no more nudges ever; their opt-out is kept.
* Moving on to another stage means they're ordering again, so any bucket
  ends. Paying stops everything.

A message also waits while the customer is chatting with the bot mid-order,
or while their chat is with your team.

## Leads

* **Bucket counts**: No response, Later, No, Dead Lead. Click one to see who
  is in it.
* **By stage**: how many customers are in each stage now and how many got
  that stage's message. Click a stage to see them.
* **Leads**: each customer the nudges are working on, with their stage, their
  bucket and messages sent, and their last message: Sent, Delivered, Read or
  Failed, and the button they tapped. **Next** shows when the next step
  happens, or why it's waiting ("Waits for the send hours", "not approved by
  WhatsApp yet", "Ordering with the bot right now").
* **Messages sent**: the latest 100, with each one's status and reply.

Delivered and Read need Gallabox's **Message.WA.Status** webhook events
switched on (Settings > Webhooks, same URL as messages). Without them a
message shows Sent or Failed.

## Zoho

With **Write to Zoho** on, each customer's Zoho Lead or Contact gets:

* a timeline note "WhatsApp nudges" for each message, tap and move;
* **Retargeting Bucket** (No-response, Later, No, Dead Lead, or Engaged once
  they're ordering again), **Retargeting Attempt Count**, **Last** and
  **Next Retargeting Date**, and **Last Template ID**.

`scripts/setup_zoho_crm.py` creates the fields (docs/zoho-setup.md). On a
CRM without them, the notes still go in.

## How it runs

The scheduler runs the nudges every 5 minutes (`workers/jobs.py`,
`send_nudges`). Each send is a row in `nudge_sends`. Each customer's place
(stage, bucket, messages sent, next step) is a row in `nudge_leads`. A send
that fails is retried on the next runs, and given up after 3 failures in a
row. At most 300 messages go per run.

The settings are one JSON setting, `NUDGE_RULES`. The earlier nudges
feature (campaigns, the 54-message library, follow-up journeys) was removed
on 31 Oct 2026; migration `0017_remove_nudges` dropped its tables.
