"""Nudges: WhatsApp messages by funnel stage, and the No / Later / No response
sequences after them, configured on the admin Nudges page.

    config.py    the rules, sequences and messages (one setting, NUDGE_RULES)
    messages.py  each message as a WhatsApp template with Order Food / Later / No
    runner.py    the five-minute run: who is due, sending, moves between buckets
    replies.py   Later / No / STOP / Order Food from the customer
    crm.py       the customer's bucket and messages on their Zoho record
    delivery.py  delivered / read receipts from Gallabox

See docs/nudges.md.
"""
