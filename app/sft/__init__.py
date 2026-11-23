"""Shero Food Training (SFT): the course's WhatsApp journey, separate from
food ordering.

A lead messages SFT (its keyword, an SFT ad, an SFT button, or SFT's own
WhatsApp number), gives their name, picks a goal, gets the brochure, and
either enrols ($99, Stripe), asks the team a question (the bot then stays
silent until the team hands the chat back), or is followed up. Every step is
a stage in Zoho (tags, Lead Source "SFT - <source>", notes, tasks), and the
team gets the leads who stall at a stage.

    config.py     the settings (SFT_CONFIG), saved on the admin page; off by default
    content.py    every message (editable on Bot replies), button, FAQ answer,
                  team script and template
    router.py     is a WhatsApp message SFT's? (asked first by the webhook)
    flow.py       the conversation
    leads.py      a lead's stage, timeline and team tasks
    sending.py    sending on the right number, logged on the timeline
    payments.py   Stripe Checkout behind one stable enrol link
    offers.py     codes and launch prices: what a lead pays
    web.py        /sft/enrol/<token>
    nudges.py     follow-ups, reminders and team tasks (job: sft_nudges)
    crm.py        Zoho sync (job: sft_crm)
    analytics.py  the KPIs
    periods.py    the admin's date ranges
    views.py      each lead's flow and next nudge, for the admin
    admin.py      /admin/sft (not in the dashboard menu yet)

Tables: sft_leads, sft_events, sft_offers (models/sft.py). See docs/sft-bot.md.
"""
