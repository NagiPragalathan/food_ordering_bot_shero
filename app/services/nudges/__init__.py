"""Retargeting nudges - abandoned carts, unpaid orders, repeat-purchase
reminders, win-backs and targeted offers - sent on WhatsApp from local data
and recorded in Zoho.

    config.py    what can be customised, validated (settings and campaigns)
    store.py     reading and saving settings and campaigns
    audience.py  who qualifies for a campaign right now, from the local database
    render.py    the message, with its {placeholders} filled in
    runner.py    the run: rules, caps, quiet hours, the 24-hour window, sending
    optout.py    STOP / START OFFERS from customers
    reports.py   the send log and conversion stats

See docs/nudges.md.
"""
