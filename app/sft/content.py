"""Everything SFT says, in one place: the stages, the bot's messages and
buttons, the FAQ answers, the team's scripts and the follow-up templates.

The facts (5 days, 10 videos of 52 minutes, 10 quizzes, 3 cuisines, the fee,
certification after approval, no promise of income or partnership) are the
brochure's. Change them here and in the brochure together.

WhatsApp limits: a reply button's title is at most 20 characters (3 buttons),
a list row's 24.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.integrations.gallabox.templates import TemplateSpec

# --- Stages: where the lead is (Zoho tags, analytics, the admin page) -----------
NEW, CONTACTED, INTERESTED = "new", "contacted", "interested"
BROCHURE_SENT, QUESTIONS, PAYMENT_PENDING = "brochure_sent", "questions", "payment_pending"
ENROLLED, NURTURE, LOST = "enrolled", "nurture", "lost"

STAGES: dict[str, str] = {
    NEW: "New",
    CONTACTED: "Contacted",
    INTERESTED: "Interested",
    BROCHURE_SENT: "Brochure Sent",
    QUESTIONS: "Questions",
    PAYMENT_PENDING: "Payment Pending",
    ENROLLED: "Paid (Enrolled)",
    NURTURE: "Nurture (Maybe Later)",
    LOST: "Lost (Not Interested)",
}
# The funnel's order; Nurture and Lost sit beside it.
FUNNEL = (NEW, CONTACTED, INTERESTED, BROCHURE_SENT, QUESTIONS, PAYMENT_PENDING, ENROLLED)
CLOSED = (ENROLLED, LOST)

# Zoho tag per stage (a tag is at most 25 characters).
STAGE_TAGS: dict[str, str] = {
    NEW: "SFT New", CONTACTED: "SFT Contacted", INTERESTED: "SFT Interested",
    BROCHURE_SENT: "SFT Brochure Sent", QUESTIONS: "SFT Questions",
    PAYMENT_PENDING: "SFT Payment Pending", ENROLLED: "SFT Enrolled",
    NURTURE: "SFT Nurture", LOST: "SFT Lost",
}
TAG_ALL = "SFT"
TAG_WITH_TEAM = "SFT With Team"
TAG_NEEDS_CALL = "SFT Needs Call"
GOAL_TAGS = {"business": "SFT Goal Business", "skills": "SFT Goal Skills"}
GOALS = {"business": "Start a food business", "skills": "Improve my skills"}

# --- Steps: what the bot is waiting for ----------------------------------------
ASK_NAME = "ask_name"          # typed: their name
GOAL = "goal"                  # Start a food business / Improve my skills
DETAILS = "details"            # View course details / Ask a question
FAQ = "faq"                    # the questions list
DECIDE = "decide"              # after the brochure: Yes / Not right now / Ask a question
QUESTION = "question"          # typed: their question, for the team
PAY = "pay"                    # payment link sent
LATER = "later"                # Maybe later: when to check back
ACCESS = "access"              # paid: have you opened the course?
LOST_REASON = "lost_reason"    # Not interested: why (optional)
WITH_TEAM = "with_team"        # a person has the chat; the bot is silent
IDLE = "idle"

TYPED_STEPS = (ASK_NAME, QUESTION)

# --- Buttons --------------------------------------------------------------------
# Every id starts with "sft:", so a tap is SFT's on a number shared with the
# ordering bot. A template's quick reply comes back as its label: LABELS.
B_BUSINESS = "sft:goal:business"
B_SKILLS = "sft:goal:skills"
B_DETAILS = "sft:details"
B_ASK = "sft:ask"
B_ENROL = "sft:enrol"
B_LATER = "sft:later"              # Not right now (after the brochure)
B_MAYBE = "sft:maybe"              # Maybe later (on a follow-up)
B_NO = "sft:no"
B_ACCESS_OK = "sft:access:ok"
B_ACCESS_HELP = "sft:access:help"
B_FAQ = "sft:faq"                      # the list again
B_WEEK, B_MONTH, B_THEM = "sft:when:week", "sft:when:month", "sft:when:them"
FAQ_IDS = ("sft:faq:learn", "sft:faq:cert", "sft:faq:partner", "sft:faq:cost", "sft:faq:own")
WHY_IDS = {"sft:why:price": "Price", "sft:why:timing": "Timing", "sft:why:other": "Something else"}


@dataclass(frozen=True)
class Button:
    id: str
    title: str


GOAL_BUTTONS = (Button(B_BUSINESS, "Start food business"), Button(B_SKILLS, "Improve my skills"))
DETAIL_BUTTONS = (Button(B_DETAILS, "View course details"), Button(B_ASK, "Ask a question"))
DECIDE_BUTTONS = (Button(B_ENROL, "Yes, enroll me"), Button(B_LATER, "Not right now"),
                  Button(B_ASK, "Ask a question"))
AFTER_FAQ_BUTTONS = (Button(B_DETAILS, "View course details"), Button(B_ENROL, "Enroll now"),
                     Button(B_FAQ, "Another question"))
ACCESS_BUTTONS = (Button(B_ACCESS_OK, "Yes, I'm in"), Button(B_ACCESS_HELP, "I need help"))
WHEN_BUTTONS = (Button(B_WEEK, "In a week"), Button(B_MONTH, "In a month"),
                Button(B_THEM, "I'll message you"))
WHY_BUTTONS = tuple(Button(i, t) for i, t in WHY_IDS.items())
PAY_BUTTON = "Pay {fee} to enroll"
COURSE_BUTTON = "Open your course"
GET_COURSE = "Get my course"          # sft_enrolled's quick reply

# What a tap on a template's quick reply (or a typed label) means.
LABELS: dict[str, str] = {
    # US spelling; matching.intent reads "enrol" (British) the same way.
    "enroll now": B_ENROL, "yes enroll me": B_ENROL, "complete enrollment": B_ENROL,
    "ask a question": B_ASK, "another question": B_FAQ,
    "view course details": B_DETAILS, "course details": B_DETAILS,
    "maybe later": B_MAYBE, "not right now": B_LATER,
    "not interested": B_NO,
    "yes i m in": B_ACCESS_OK, "i need help": B_ACCESS_HELP,
    # sft_enrolled: a paid lead gets the course and the receipt (an unpaid
    # one the checkout), as Enroll now does.
    "get my course": B_ENROL,
    "start food business": B_BUSINESS, "start a food business": B_BUSINESS,
    "improve my skills": B_SKILLS,
    "in a week": B_WEEK, "in a month": B_MONTH, "i ll message you": B_THEM,
}

# --- The messages ----------------------------------------------------------------
ASK_NAME_TEXT = ("Hi there! 👋 Welcome to *Shero Food Training (SFT)*.\n\n"
                 "Before we begin, may I know your name?")
ASK_NAME_AGAIN = "Sorry, I didn't catch that. What's your name? (Just your first name is fine.)"

WELCOME = ("Hi {first}! 👋\n\n"
           "Welcome to *Shero Food Training (SFT)*: a 5-day, self-paced course that teaches "
           "you to cook to Shero's standards and get certified.\n\n"
           "🎥 10 short videos  ·  📝 10 quizzes  ·  🍛 3 regional cuisines\n"
           "💲 One-time course fee: *{fee}*\n\n"
           "What would you like to achieve?")
WELCOME_BACK = "Welcome back, {first}! 👋 Happy to pick up where we left off."

INTEREST = {
    "business": ("That's a great goal, {first}! 💼\n\n"
                 "SFT shows you how food is prepared for customers the Shero way: ingredients, "
                 "storage, base preparation, hygiene, packing, labels, allergens, order execution "
                 "and the customer handoff, as one repeatable process.\n\n"
                 "Would you like to see the course details?"),
    "skills": ("That's a great goal, {first}! 👩‍🍳\n\n"
               "SFT helps you build your South Indian food skills step by step: ingredients, "
               "storage, base masala preparation, hygiene and packing, in short video lessons "
               "with a quiz after each.\n\n"
               "Would you like to see the course details?"),
}

BROCHURE_CAPTION = "Shero Food Training (SFT) brochure"
BROCHURE_FILENAME = "Shero-Food-Training-SFT-Brochure.pdf"
BROCHURE = ("Here's the SFT brochure 📄\n\n"
            "Inside: the 5-day plan, all 10 video lessons and quizzes, what your {fee} covers "
            "and how certification works.\n\n"
            "Would you like to enroll now?")
BROCHURE_LINK_LINE = "\n\n(Can't open the PDF? {url})"

ASK_QUESTION = ("Of course, {first}! Please type your question below 👇 and a member of our "
                "team will answer you right here.")
HANDED_OVER = ("Thank you, {first}! 🙏 We're transferring you to a member of our team, who "
               "will reply to you here shortly.")

ENROL = ("Wonderful, {first}! 🎉 You're ready to start Shero Food Training.\n\n"
         "*Course fee: {fee}* (one-time)\n\n"
         "Tap below to review the course details and terms, then pay securely to enroll.")
PAY_REMINDER = ("Hi {first}, your enrollment isn't complete yet. Your secure checkout is below; "
                "it only takes a minute. 🙂")
ALREADY_PAID = "You're already enrolled, {first}! 🎉 Here's your course again."
# The caption on the receipt PDF sent after paying.
RECEIPT_CAPTION = "Your enrollment receipt 🧾 No. {number}"

PAID = ("Congratulations, {first}! 🎉 Your enrollment payment of {amount} is confirmed. "
        "Welcome to Shero Food Training!\n\n"
        "Your next step is to open the course and start Day 1.{note}")
PAID_NO_LINK = ("Congratulations, {first}! 🎉 Your enrollment payment of {amount} is confirmed. "
                "Welcome to Shero Food Training!\n\n"
                "Our team will send your course access details here shortly.")
PAYMENT_FAILED = ("Hi {first}, your payment didn't go through, so you haven't been charged. "
                  "You can try again below, or tap Ask a question and we'll help.")

ACCESS_CHECK = "Hi {first}! Have you been able to open your Shero Food Training course? 🎓"
ACCESS_OK = ("Great to hear, {first}! 🙌 Enjoy Day 1. Complete the lessons and quizzes in "
             "order, and message us here any time you need help.")
ACCESS_HELP = ("Sorry about that, {first}! We're transferring you to a member of our team, who "
               "will help you get into the course here shortly.")

LATER_TEXT = ("No problem, {first}! 👍 When would be a good time for us to check back?")
LATER_SET = ("Done! We'll check in {when}. Whenever you're ready, just reply *SFT* here to "
             "pick up where you left off.")
LATER_THEM = ("Sure! We won't chase you. Whenever you're ready, just reply *SFT* here to pick "
              "up where you left off. 🙂")
NOT_NOW = ("No problem, {first}! 👍 We'll keep you updated with important information. "
           "Whenever you're ready, just reply *SFT* here to pick up where you left off.")

NOT_INTERESTED = ("Thanks for letting us know, {first}. We won't send you any more messages "
                  "about the course.\n\n"
                  "If you have a moment, what's the main reason? It helps us improve.")
LOST_THANKS = "Thank you, that's noted. 🙏 If you change your mind, just reply *SFT* any time."
STOPPED = ("Done. You won't get any more messages about Shero Food Training. If you change "
           "your mind, just reply *SFT* any time.")

TAP_BELOW = "Please tap one of the options below 👇"

FAQ_PROMPT = "What would you like to know, {first}?"
FAQ_BUTTON = "See questions"
FAQ_ROWS: tuple[tuple[str, str, str], ...] = (
    ("sft:faq:learn", "What will I learn?", "The 5-day plan, videos and quizzes"),
    ("sft:faq:cert", "Do I get a certificate?", "How certification works"),
    ("sft:faq:partner", "Kitchen Partner pathway", "What happens after the course"),
    ("sft:faq:cost", "How much does it cost?", "The course fee and what it covers"),
    ("sft:faq:own", "Ask my own question", "Type it and our team will reply"),
)
FAQ_ANSWERS: dict[str, str] = {
    "sft:faq:learn": (
        "📚 *What you'll learn*\n\n"
        "A guided 5-day sequence: 10 short video lessons (52 minutes in all) and 10 quizzes, "
        "one mid-day and one end-of-day each day.\n\n"
        "Day 1 · Meet Shero Food Technology\n"
        "Day 2 · Understand the process\n"
        "Day 3 · Ingredients and base preparation\n"
        "Day 4 · Hygiene, packing, labels and allergens\n"
        "Day 5 · Order execution and the daily operating rhythm\n\n"
        "It's self-paced and online: learn on your own schedule on a laptop or desktop."),
    "sft:faq:cert": (
        "🎓 *Certification*\n\n"
        "The certificate isn't automatic on payment. After the course you prepare your assigned "
        "base items and products with the standardized recipe, upload photos through the app, "
        "and Shero reviews them. The certificate is issued once the required approval is "
        "granted."),
    "sft:faq:partner": (
        "🤝 *Kitchen Partner pathway*\n\n"
        "SFT teaches Shero's methods and the steps for product preparation and approval. "
        "Becoming a Shero Kitchen Partner is a separate step that depends on Shero's review, "
        "standards, eligibility and local requirements.\n\n"
        "The course doesn't guarantee partner acceptance, orders or income."),
    "sft:faq:cost": (
        "💲 *Course fee: {fee}, one time*\n\n"
        "It covers the 5-day guided sequence of 10 video lessons, the 10 knowledge checks, the "
        "food-operations topics (ingredients, storage, base preparation, hygiene, packing, "
        "labels, allergens, order execution and customer experience) and the defined next step "
        "into the approval stage."),
}

# --- The team's scripts (the drop-off table) -------------------------------------------
@dataclass(frozen=True)
class Drop:
    """One drop-off: when the team takes over, why, and what to say."""
    key: str
    stage_name: str
    trigger: str
    script: str
    next_action: str
    priority: str = "normal"


DROPS: dict[str, Drop] = {
    NEW: Drop(NEW, "Lead Enters", "No option chosen within {hours} hours",
              "Hi {first}, saw your enquiry about SFT. What are you looking to achieve: learn "
              "cooking, improve your skills or start a food business?",
              "Capture their interest"),
    CONTACTED: Drop(CONTACTED, "Lead Enters (replied)",
                    "Replied, but no option chosen within {hours} hours",
                    "Hi {first}, saw your enquiry about SFT. What are you looking to achieve: "
                    "learn cooking, improve your skills or start a food business?",
                    "Capture their interest"),
    INTERESTED: Drop(INTERESTED, "Choose Interest", "Chose a goal, no further step in {hours} hours",
                     "You were interested in {goal}. Let me share how SFT helps you with that.",
                     "Share the brochure"),
    BROCHURE_SENT: Drop(BROCHURE_SENT, "Send Brochure", "Brochure sent, no answer in {hours} hours",
                        "Did you get a chance to see the SFT brochure? Do you have any questions?",
                        "Clarify, then enroll"),
    QUESTIONS: Drop(QUESTIONS, "Handle Questions", "Question unresolved or no answer in {hours} hours",
                    "I can help with your question about the curriculum, certificate, partner "
                    "pathway or fee.",
                    "Resolve the doubt, then enroll"),
    PAYMENT_PENDING: Drop(PAYMENT_PENDING, "Enrollment Intent",
                          "Payment link sent, not paid in {hours} hours",
                          "I noticed your enrollment is not yet complete. Any help needed with the "
                          "payment?",
                          "Help them pay", "high"),
    "access": Drop("access", "Payment Success", "Paid, course access not confirmed in {hours} hours",
                   "Congratulations again! Have you been able to access your course? I can help "
                   "if needed.",
                   "Make sure they can open the course"),
    "followups_done": Drop("followups_done", "Follow-Up", "No answer to any of the automated "
                           "follow-ups ({hours} hours after the last)",
                           "Just checking if you're still interested in SFT or need more "
                           "information.",
                           "Resume, nurture or close"),
    NURTURE: Drop(NURTURE, "Maybe Later", "Said Maybe later",
                  "Would you like us to follow up at a more convenient time?",
                  "Set a follow-up date"),
    "question": Drop("question", "Asked a question", "Asked the team a question",
                     "Answer their question here in WhatsApp, then hand the chat back to the bot.",
                     "Answer, then hand back", "high"),
    "access_help": Drop("access_help", "Payment Success", "Paid, needs help opening the course",
                        "Sorry you had trouble opening the course. Let me help you get in.",
                        "Get them into the course", "high"),
    "course_link": Drop("course_link", "Payment Success", "Paid, but no course link is set up",
                        "Congratulations on enrolling! Here are your course access details.",
                        "Send the course access", "high"),
}

# --- Follow-up templates (outside WhatsApp's 24-hour window) ------------------------------
FOLLOWUP_1 = TemplateSpec(
    name="sft_followup_1", category="MARKETING",
    trigger="SFT: no answer for a day (first follow-up)",
    params=("first_name",),
    sample_body=("Hi {{1}}, just checking in about Shero Food Training (SFT). Did you get a "
                 "chance to look at the course? If you have any questions, we're happy to help."),
    button_kind="none", quick_replies=("Enroll now", "Ask a question", "Not interested"))
FOLLOWUP_2 = TemplateSpec(
    name="sft_followup_2", category="MARKETING",
    trigger="SFT: second follow-up (48-72 hours)",
    params=("first_name",),
    sample_body=("Hi {{1}}, a quick reminder: Shero Food Training is a 5-day, self-paced course "
                 "with 10 short videos, 10 quizzes and 3 regional cuisines, for a one-time fee "
                 "of $99. Would you like to enroll?"),
    button_kind="none", quick_replies=("Enroll now", "Maybe later", "Not interested"))
FOLLOWUP_3 = TemplateSpec(
    name="sft_followup_3", category="MARKETING",
    trigger="SFT: last follow-up (5-7 days)",
    params=("first_name",),
    sample_body=("Hi {{1}}, this is our last check-in about Shero Food Training. No pressure: if "
                 "now isn't the right time, tap Maybe later and we'll stay in touch."),
    button_kind="none", quick_replies=("Enroll now", "Maybe later", "Not interested"))
PAYMENT_REMINDER = TemplateSpec(
    name="sft_payment_reminder", category="UTILITY",
    trigger="SFT: payment link sent, not paid",
    params=("first_name",),
    sample_body=("Hi {{1}}, your Shero Food Training enrollment isn't complete yet. Tap Complete "
                 "enrollment to get your secure checkout link again."),
    button_kind="none", quick_replies=("Complete enrollment", "Ask a question"))
COURSE_CHECK = TemplateSpec(
    name="sft_course_check", category="UTILITY",
    trigger="SFT: a day after paying",
    params=("first_name",),
    sample_body=("Hi {{1}}, have you been able to open your Shero Food Training course? Tap below "
                 "and let us know."),
    button_kind="none", quick_replies=("Yes, I'm in", "I need help"))
NURTURE_TEMPLATE = TemplateSpec(
    name="sft_nurture", category="MARKETING",
    trigger="SFT: Maybe later check-in",
    params=("first_name",),
    sample_body=("Hi {{1}}, Shero Food Training is ready whenever you are: a 5-day, self-paced "
                 "course for a one-time $99. Would you like to enroll now?"),
    button_kind="none", quick_replies=("Enroll now", "Maybe later", "Not interested"))
TEAM_ALERT = TemplateSpec(
    name="sft_team_alert", category="UTILITY",
    trigger="SFT: a lead wants a person (to the team's numbers)",
    params=("lead_name", "phone", "reason"),
    sample_body=("SFT lead {{1}} ({{2}}) needs the team: {{3}}. Please reply to them in "
                 "Gallabox."),
    button_kind="none")

# Paying or failing to pay after WhatsApp's 24-hour window: the confirmation
# (and the failure) can only go as templates. A tap on their button opens the
# window again, and the bot answers it with the course and the receipt PDF
# (or the checkout link again).
ENROLLED_TEMPLATE = TemplateSpec(
    name="sft_enrolled", category="UTILITY",
    trigger="SFT: payment confirmed outside WhatsApp's 24-hour window",
    params=("first_name", "amount", "receipt_number"),
    sample_body=("Hi {{1}}, your payment of {{2}} for Shero Food Training is confirmed "
                 "(receipt no. {{3}}). Welcome to SFT! Tap Get my course for your course access "
                 "and your receipt."),
    button_kind="none", quick_replies=(GET_COURSE,))
PAYMENT_FAILED_TEMPLATE = TemplateSpec(
    name="sft_payment_failed", category="UTILITY",
    trigger="SFT: payment failed outside WhatsApp's 24-hour window",
    params=("first_name",),
    sample_body=("Hi {{1}}, your payment for Shero Food Training didn't go through, so you "
                 "haven't been charged. Tap Complete enrollment to get your secure checkout "
                 "link again."),
    button_kind="none", quick_replies=("Complete enrollment", "Ask a question"))

TEMPLATES: tuple[TemplateSpec, ...] = (FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3, PAYMENT_REMINDER,
                                       COURSE_CHECK, NURTURE_TEMPLATE, TEAM_ALERT,
                                       ENROLLED_TEMPLATE, PAYMENT_FAILED_TEMPLATE)
FOLLOWUPS: tuple[TemplateSpec, ...] = (FOLLOWUP_1, FOLLOWUP_2, FOLLOWUP_3)


def first_name(name: str | None, fallback: str = "there") -> str:
    parts = (name or "").split()
    return parts[0][:40] if parts else fallback


OFFER_APPLIED = ("🎉 Code *{code}* applied, {first}! Your course fee is now *{fee}* (was {was}).")

# What the bot is waiting for, as the admin pages say it.
STEP_LABELS: dict[str, str] = {
    ASK_NAME: "their name", GOAL: "a goal", DETAILS: "course details or a question",
    FAQ: "a question from the list", DECIDE: "a decision after the brochure",
    QUESTION: "their question", PAY: "payment", LATER: "when to check back",
    ACCESS: "course access", LOST_REASON: "why not", WITH_TEAM: "the team", IDLE: "",
}


# --- The bot's wording, editable on the Bot replies page ----------------------------
@dataclass(frozen=True)
class Text:
    key: str
    label: str
    group: str
    default: str
    # The {placeholders} it may use; each is filled in for the lead.
    fields: tuple[str, ...] = ()
    note: str = ""


# Interactive messages carry at most 1024 characters.
TEXT_LIMIT = 1024
FIELD_HELP = {"first": "their first name", "fee": "the course fee (with any offer)",
              "amount": "what they paid", "note": "the course access note (Settings)",
              "when": "in a week / in a month", "code": "the offer code",
              "was": "the fee before the offer"}

TEXTS: dict[str, Text] = {t.key: t for t in (
    Text("ask_name", "Ask their name", "1 · Lead enters", ASK_NAME_TEXT),
    Text("ask_name_again", "Name not understood", "1 · Lead enters", ASK_NAME_AGAIN),
    Text("welcome", "Welcome (with the goal buttons)", "1 · Lead enters", WELCOME,
         ("first", "fee")),
    Text("welcome_back", "Welcome back", "1 · Lead enters", WELCOME_BACK, ("first",)),
    Text("interest_business", "Goal: start a food business", "2 · Choose interest",
         INTEREST["business"], ("first",)),
    Text("interest_skills", "Goal: improve my skills", "2 · Choose interest",
         INTEREST["skills"], ("first",)),
    Text("brochure", "After the brochure (Yes / Not right now / Ask)", "3 · Brochure",
         BROCHURE, ("fee",), "The PDF's link is added under it."),
    Text("not_now", "Not right now", "3 · Brochure", NOT_NOW, ("first",)),
    Text("ask_question", "Ask a question: type it", "3 · Brochure", ASK_QUESTION, ("first",)),
    Text("handed_over", "Transferring to the team", "3 · Brochure", HANDED_OVER, ("first",)),
    Text("faq_prompt", "The questions list", "4 · Questions", FAQ_PROMPT, ("first",)),
    Text("faq_learn", "What will I learn?", "4 · Questions", FAQ_ANSWERS["sft:faq:learn"]),
    Text("faq_cert", "Do I get a certificate?", "4 · Questions", FAQ_ANSWERS["sft:faq:cert"]),
    Text("faq_partner", "Kitchen Partner pathway", "4 · Questions",
         FAQ_ANSWERS["sft:faq:partner"]),
    Text("faq_cost", "How much does it cost?", "4 · Questions", FAQ_ANSWERS["sft:faq:cost"],
         ("fee",)),
    Text("enrol", "Enroll: the payment link", "5 · Payment", ENROL, ("first", "fee")),
    Text("pay_reminder", "Payment link again (a nudge)", "5 · Payment", PAY_REMINDER,
         ("first",)),
    Text("payment_failed", "Payment failed", "5 · Payment", PAYMENT_FAILED, ("first",)),
    Text("offer_applied", "Offer code applied", "5 · Payment", OFFER_APPLIED,
         ("first", "code", "fee", "was")),
    Text("paid", "Payment confirmed (with the course button)", "6 · Paid", PAID,
         ("first", "amount", "note")),
    Text("paid_no_link", "Payment confirmed (no course link set)", "6 · Paid", PAID_NO_LINK,
         ("first", "amount")),
    Text("already_paid", "Already enrolled", "6 · Paid", ALREADY_PAID, ("first",)),
    Text("access_ok", "Opened the course", "6 · Paid", ACCESS_OK, ("first",)),
    Text("access_help", "Needs help opening the course", "6 · Paid", ACCESS_HELP, ("first",)),
    Text("later", "Maybe later: when?", "7 · Follow-ups", LATER_TEXT, ("first",)),
    Text("later_set", "Check back set", "7 · Follow-ups", LATER_SET, ("when",)),
    Text("later_them", "They will message us", "7 · Follow-ups", LATER_THEM),
    Text("not_interested", "Not interested (asks why)", "8 · Stop", NOT_INTERESTED, ("first",)),
    Text("lost_thanks", "Thanks for the reason", "8 · Stop", LOST_THANKS),
    Text("stopped", "STOP", "8 · Stop", STOPPED),
    Text("tap_below", "Please tap an option", "Any step", TAP_BELOW),
)}
# The buttons each message goes out with (shown with it on Bot replies).
TEXT_BUTTONS: dict[str, tuple[str, ...]] = {
    "welcome": tuple(b.title for b in GOAL_BUTTONS),
    "interest_business": tuple(b.title for b in DETAIL_BUTTONS),
    "interest_skills": tuple(b.title for b in DETAIL_BUTTONS),
    "brochure": tuple(b.title for b in DECIDE_BUTTONS),
    "faq_prompt": (FAQ_BUTTON,),
    **{key: tuple(b.title for b in AFTER_FAQ_BUTTONS)
       for key in ("faq_learn", "faq_cert", "faq_partner", "faq_cost")},
    "enrol": (PAY_BUTTON.format(fee="{fee}"),),
    "pay_reminder": (PAY_BUTTON.format(fee="{fee}"),),
    "payment_failed": ("Try again",),
    "paid": (COURSE_BUTTON,),
    "already_paid": (COURSE_BUTTON,),
    "later": tuple(b.title for b in WHEN_BUTTONS),
    "not_interested": tuple(b.title for b in WHY_BUTTONS),
}

FAQ_TEXTS = {"sft:faq:learn": "faq_learn", "sft:faq:cert": "faq_cert",
             "sft:faq:partner": "faq_partner", "sft:faq:cost": "faq_cost"}


def say(cfg, key: str, **values) -> str:
    """The bot's text `key` (edited on the Bot replies page, or the default),
    filled in. An edit that can't be filled in falls back to the default."""
    spec = TEXTS[key]
    edited = (getattr(cfg, "texts", None) or {}).get(key)
    for raw in (edited, spec.default):
        if not raw:
            continue
        try:
            return raw.format(**values)
        except (KeyError, IndexError, ValueError, AttributeError):
            continue
    return spec.default


def check_text(key: str, value: str) -> str | None:
    """Why an edited text can't be saved, or None."""
    import string

    spec = TEXTS.get(key)
    if spec is None:
        return "unknown message"
    if not value.strip():
        return "the message can't be empty"
    if len(value) > TEXT_LIMIT:
        return f"at most {TEXT_LIMIT} characters (this one has {len(value)})"
    try:
        used = {name for _, name, _, _ in string.Formatter().parse(value) if name is not None}
    except ValueError:
        return "a { or } is not closed: write {first}, {fee}… exactly, or leave braces out"
    unknown = sorted(n for n in used if n not in spec.fields)
    if unknown:
        allowed = ", ".join("{" + f + "}" for f in spec.fields) or "none"
        return (f"{', '.join('{' + n + '}' for n in unknown)} can't be filled in here "
                f"(this message can use: {allowed})")
    return None
