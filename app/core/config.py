"""Typed application settings.

Everything the service needs comes from the environment (see `.env.example`).
Nothing is hardcoded: credentials, base URLs and business rules are all here so
a deployment can be re-pointed (test -> live Stripe, zoho.com -> zoho.in)
without touching code.

There is one settings object per business (app/core/tenants.py): Shero US
reads the plain variables, DST India the DST_ ones. `settings` below is
whichever business is being served, so code reads `settings.x` the same way
for both.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import (BaseSettings, DotEnvSettingsSource, EnvSettingsSource,
                               SettingsConfigDict)

Environment = Literal["development", "staging", "production"]
SourceMode = Literal["local", "remote"]
# How a customer browses: a link to the web storefront, or WhatsApp lists.
OrderMode = Literal["web", "chat"]
DistanceMode = Literal["haversine", "google_matrix"]

# Zoho runs region-isolated stacks; the OAuth and API hosts must match the data
# centre the client's org actually lives in, otherwise every call 401s.
ZOHO_HOSTS: dict[str, tuple[str, str]] = {
    "com": ("https://accounts.zoho.com", "https://www.zohoapis.com"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
}


def is_unset(value: str | None) -> bool:
    """True when a setting is blank or is obviously a placeholder.

    python-dotenv keeps everything after `=` as the value, so a line like
    `KEY=    # [REQUIRED] paste it here` silently sets KEY to that comment.
    Without this check the readiness probe would report a completely
    unconfigured deployment as ready.
    """
    text = (value or "").strip()
    if not text:
        return True
    # A comment that leaked in as a value.
    if text.startswith("#"):
        return True
    # Common placeholder spellings left behind in a copied template.
    lowered = text.lower()
    return lowered in {
        "changeme", "change-me", "todo", "tbd", "xxx", "none", "null",
        "your-key-here", "<your-key>", "placeholder",
    }


class Settings(BaseSettings):
    """Shero US's settings, and the shape of every business's (DstSettings)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- The business (app/core/tenants.py) ----------------------------------
    business_name: str = "Shero US"
    # Money, places and phone numbers in this business's own terms.
    currency: str = "USD"
    currency_symbol: str = "$"
    country_code: str = "US"
    # Put in front of a 10-digit number typed without its country code.
    phone_country_code: str = "1"
    # What the admin pages and customers read distances in. Stored as km.
    distance_unit: Literal["mi", "km"] = "mi"
    postal_code_label: str = "ZIP code"
    postal_code_digits: int = Field(default=5, ge=3, le=10)
    # A new kitchen's timezone until its map pin says otherwise.
    default_timezone: str = "America/New_York"
    # Who takes the payment, and who carries the food.
    payment_provider: Literal["stripe", "razorpay"] = "stripe"
    delivery_provider: Literal["uber", "own"] = "uber"
    # WhatsApp template names are unique across a Gallabox account, so a second
    # business's copy of the same message is named with a prefix ("dst_").
    template_prefix: str = ""
    # Order numbers: SHO-261116-K4T9P. Each business numbers its own orders, so
    # the prefix tells the team (and Razorpay or Stripe) whose order it is.
    order_number_prefix: str = Field(default="SHO", pattern=r"^[A-Z]{2,5}$")

    # --- Application ---------------------------------------------------------
    app_env: Environment = "development"
    log_level: str = "INFO"
    enable_scheduler: bool = True
    public_base_url: str = "http://localhost:8000"
    pay_redirect_base_url: str = "http://localhost:8000"

    # --- Database ------------------------------------------------------------
    # Any Postgres URL works (postgres://, postgresql://, ?sslmode=require as
    # Neon/Vercel give it); app/db/url.py turns it into the asyncpg form.
    database_url: str = "postgresql+asyncpg://shero:shero@localhost:5432/shero_bot"

    # --- Hosting on Vercel (docs/vercel-hosting.md) ---------------------------
    # Set to "1" by Vercel itself. Serverless: no in-process scheduler (Vercel
    # Cron calls /cron/*), no connection pool kept between requests, files
    # written only under /tmp.
    vercel: str = ""
    # Vercel sends it as "Authorization: Bearer <CRON_SECRET>" on every cron
    # call; /cron/* refuses any other caller, and refuses everyone when unset.
    cron_secret: str = ""
    # Set by Vercel when a Blob store is connected. With it, dish photos are
    # stored in Vercel Blob instead of on disk (services/media.py).
    blob_read_write_token: str = ""
    # Where dish photos are written when Blob is not used. Default data/media;
    # /tmp/shero-media on Vercel.
    media_dir: str = ""

    # --- Gallabox ------------------------------------------------------------
    gallabox_api_key: str = ""
    gallabox_api_secret: str = ""
    gallabox_channel_id: str = ""
    # Only needed to create/inspect message templates, not to send messages.
    gallabox_account_id: str = ""
    gallabox_base_url: str = "https://server.gallabox.com/devapi"
    gallabox_webhook_token: str = ""
    # The business's own WhatsApp number, digits with country code
    # (e.g. 14438011011). Powers the "Open WhatsApp" button after a web
    # order; left empty, the page simply does not show it.
    whatsapp_business_number: str = ""

    # --- Ops API (outlet staff marking orders) -------------------------------
    ops_api_key: str = ""

    # --- Integration API (read-only: catalogue and orders) --------------------
    # Each business its own (DST_INTEGRATION_API_KEY for DST India), made on its
    # admin Settings page. Empty: /api/v1 answers 503 and gives nothing out.
    integration_api_key: str = ""

    # --- Shero Training course platform (Shero Food Training, Shero US) -------
    # The secret its payment webhook checks (x-webhook-secret), from Shero.
    # Pasted on SFT's Settings page; empty: no course accounts are made.
    sft_platform_webhook_secret: str = ""

    # --- Admin dashboard -----------------------------------------------------
    admin_session_secret: str = ""
    admin_session_hours: int = 12
    # Fernet key encrypting secrets saved through the Settings page.
    settings_encryption_key: str = ""
    # Bootstrap login, used once to create the first admin user.
    admin_bootstrap_email: str = ""
    admin_bootstrap_password: str = ""

    # --- Meta ----------------------------------------------------------------
    meta_graph_version: str = "v21.0"
    meta_catalog_id: str = ""
    meta_system_user_token: str = ""
    # The WhatsApp Business Account id, for scripts/submit_templates.py --via-meta.
    meta_waba_id: str = ""
    meta_catalog_cache_ttl_seconds: int = 900

    # --- Zoho ----------------------------------------------------------------
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_refresh_token: str = ""
    zoho_data_center: str = "com"
    zoho_orders_module: str = "Orders"
    # The Orders module's display field, which holds the order number: "Name"
    # unless the module was created by hand with another (setup script warns).
    zoho_orders_name_field: str = "Name"
    # The bot's line-items module (one record per dish on a paid order).
    zoho_order_items_module: str = "Order_Items"
    # Which Zoho org the bot is connected to, recorded by Connect Zoho. Record
    # ids belong to one org; connecting another drops the saved links.
    zoho_org_id: str = ""
    # Zoho is this business's CRM, so the readiness check wants its keys.
    # DST India's CRM is its own; Zoho there is optional.
    zoho_required: bool = True
    # Only a dish with a price above zero can be ordered. Off for Shero US,
    # which has always sold every dish switched on; on for DST India, whose
    # menu comes from the ERP, where a dish can have no Mrp.
    price_required: bool = False

    # --- Uber Direct ---------------------------------------------------------
    uber_customer_id: str = ""
    uber_client_id: str = ""
    uber_client_secret: str = ""
    uber_scope: str = "eats.deliveries"
    uber_auth_url: str = "https://auth.uber.com/oauth/v2/token"
    uber_api_base_url: str = "https://api.uber.com"
    # Book the courier this long before the delivery slot starts, so they
    # can collect from the kitchen and arrive inside the slot.
    uber_dispatch_hours_before: float = Field(default=2.0, gt=0)

    # --- Stripe --------------------------------------------------------------
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_currency: str = "usd"
    payment_link_ttl_minutes: int = 30
    payment_reminder_minutes: int = 15

    # --- Razorpay (payment_provider=razorpay) ---------------------------------
    # Dashboard -> Account & Settings -> API Keys. rzp_test_... then rzp_live_...
    # Left blank, checkout is mocked like Stripe's: a test page, no charge.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    # Dashboard -> Webhooks: the secret set on our endpoint (event payment_link.paid).
    razorpay_webhook_secret: str = ""
    razorpay_base_url: str = "https://api.razorpay.com/v1"

    # --- Own delivery staff (delivery_provider=own) ---------------------------
    # The business's own riders carry the food, so the charge is its own: a
    # flat fee, free from a dish total upwards (0 = never free), within a
    # straight-line distance of the kitchen.
    own_delivery_fee: float = Field(default=0.0, ge=0)
    free_delivery_above: float = Field(default=0.0, ge=0)
    own_delivery_max_km: float = Field(default=10.0, gt=0, le=100)

    # --- Shiprocket (riders for own delivery, booked from "Mark ready") ---------
    # An API user from Shiprocket > Settings > API Users (its email and the
    # password set there). Empty: no Mark ready button, the own riders deliver.
    # DST_SHIPROCKET_EMAIL / DST_SHIPROCKET_PASSWORD (services/shiprocket_booking.py).
    shiprocket_email: str = ""
    shiprocket_password: str = ""
    shiprocket_base_url: str = "https://apiv2.shiprocket.in/v1/external"
    # The parcel Shiprocket is told about: one food order, in kg.
    shiprocket_weight_kg: float = Field(default=1.0, gt=0, le=20)

    # --- DST India's own CRM (its Lead Pipeline & Tracker) ---------------------
    # The CRM's ordering-bot endpoint (services/dst_crm.py): its address, e.g.
    # https://dst.shero.website, and the shared key (ORDERING_BOT_TOKEN on the
    # CRM's server). Empty: nothing is sent. DST_CRM_API_URL / _TOKEN.
    crm_api_url: str = ""
    crm_api_token: str = ""

    # --- Optional customer messages ------------------------------------
    # Both are in the original spec but switched off at the client's
    # request. The templates stay approved on the WABA either way, so
    # turning one back on is this flag and nothing else - no resubmission.
    # Off means the *message* is skipped, not the step: an unpaid order
    # still expires, and an order still moves through the kitchen stages.
    send_payment_reminder: bool = False
    send_delivery_updates: bool = False
    # "Your order is being prepared", right after the payment confirmation.
    # DST India's (on by default there); Shero US's kitchens cook a day ahead.
    send_preparing_message: bool = False

    # --- Who the bot answers ---------------------------------------------
    # Set ONLY on the admin Bot replies page (stored in the database), never
    # from .env: the alias below is a name no environment uses, so a stray
    # BOT_ALLOWED_NUMBERS line cannot restrict the bot. Nothing saved means
    # the bot replies to everyone. See services/allowlist.py.
    bot_reply_mode: str = Field(default="", validation_alias="ADMIN_PAGE_ONLY_BOT_REPLY_MODE")
    # Whitelisted WhatsApp numbers, "number|name,...".
    bot_allowed_numbers: str = Field(default="",
                                     validation_alias="ADMIN_PAGE_ONLY_BOT_ALLOWED_NUMBERS")
    # Reply to any message ("any", the default) or only start on a trigger
    # keyword ("keywords"), and the keywords as JSON. Admin page only, like
    # the two above. See services/reply_triggers.py.
    bot_reply_trigger: str = Field(default="", validation_alias="ADMIN_PAGE_ONLY_BOT_REPLY_TRIGGER")
    bot_trigger_keywords: str = Field(default="",
                                      validation_alias="ADMIN_PAGE_ONLY_BOT_TRIGGER_KEYWORDS")
    # The welcome menu sent to a new chat in keyword mode (on/off, text and
    # buttons) as JSON. Admin page only. See services/welcome_menu.py.
    bot_welcome_menu: str = Field(default="", validation_alias="ADMIN_PAGE_ONLY_BOT_WELCOME_MENU")
    # Ask a new customer for their email after their name: "on" (also when
    # nothing is saved) or "off". Bot replies page only, like the above.
    # See services/conversation/handlers/onboarding.py.
    ask_customer_email: str = Field(default="",
                                    validation_alias="ADMIN_PAGE_ONLY_ASK_CUSTOMER_EMAIL")
    # Test payments: "on" lets the bot's test numbers (Bot replies list) skip
    # the payment gateway - Pay Now opens the test page that marks the order
    # paid, nobody is charged. Everyone else still pays. Settings page only.
    test_payments: str = Field(default="", validation_alias="ADMIN_PAGE_ONLY_TEST_PAYMENTS")

    # --- Team alerts -----------------------------------------------------
    # WhatsApp numbers told about every paid order, "number|name,...". Set on
    # the admin Settings page (Order alerts) only. See services/order_alerts.py.
    order_alert_numbers: str = Field(default="",
                                     validation_alias="ADMIN_PAGE_ONLY_ORDER_ALERT_NUMBERS")

    # --- Geo -----------------------------------------------------------------
    google_maps_api_key: str = ""
    # A second key for the map drawn in the customer's browser. Anything a
    # page loads is public, so this one should be restricted in Google Cloud
    # to HTTP referrers (your domain) and the Maps JavaScript API only. The
    # server key above stays private. Empty: the page draws an OpenStreetMap
    # map instead, while addresses still come from Google via the server.
    google_maps_browser_key: str = ""
    distance_mode: DistanceMode = "haversine"
    geocoder_country: str = "US"

    # --- Client backend adapters ---------------------------------------------
    order_mode: OrderMode = "web"
    # Once the customer's details are in (and when a returning customer
    # starts again): Shero US greets them with Order Now (its website) and
    # Continue on WhatsApp; DST India goes straight to the menu, which is
    # what Continue on WhatsApp opens (onboarding.show_main_menu).
    straight_to_menu: bool = False
    outlet_source: SourceMode = "local"
    slot_source: SourceMode = "local"
    client_backend_base_url: str = ""
    client_backend_api_key: str = ""

    # --- Business rules ------------------------------------------------------
    # There is no stand-in delivery fee: a customer pays Uber's own quote, or
    # does not order (services/pricing.py).
    tax_percent: float = Field(default=0.0, ge=0, le=100)
    default_delivery_radius_km: float = Field(default=10.0, gt=0)
    # The furthest any kitchen delivers, in straight-line miles, whatever its
    # own radius says: Uber Direct's limit on this account (it refused an
    # address 23.71 miles out with "Max Radius: 10.00 miles"). It can be set
    # lower, never higher than 10, and never switched off.
    delivery_max_miles: float = Field(default=10.0, gt=0, le=10)
    slot_hold_minutes: int = 30
    # The earliest delivery slot offered starts this long after ordering:
    # home-style food is cooked to order, so the kitchen needs the day.
    slot_min_lead_hours: float = Field(default=24.0, ge=0)
    # Orders for now, as on any food delivery app: the delivery window under
    # way when the food would arrive is offered as "Now", along with every
    # later window, and the lead time is instant_lead_minutes (cooking and
    # riding) instead of SLOT_MIN_LEAD_HOURS. DST India's own riders; Shero
    # US books Uber ahead, so it stays on its day's notice.
    instant_orders: bool = False
    instant_lead_minutes: int = Field(default=45, ge=10, le=240)
    # The kitchen's new-order alert goes out at this hour (kitchen local
    # time) on the delivery day - never later than the Uber booking.
    kitchen_alert_hour: float = Field(default=7.0, ge=0, lt=24)
    feedback_delay_minutes: int = 30

    # --- Derived -------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def zoho_accounts_url(self) -> str:
        return ZOHO_HOSTS.get(self.zoho_data_center, ZOHO_HOSTS["com"])[0]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def zoho_api_url(self) -> str:
        return ZOHO_HOSTS.get(self.zoho_data_center, ZOHO_HOSTS["com"])[1]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def asks_customer_email(self) -> bool:
        """Whether the bot asks new customers for their email (Bot replies page)."""
        return self.ask_customer_email.strip().lower() != "off"

    @property
    def test_payments_on(self) -> bool:
        """The bot's test numbers skip the payment gateway (Settings page)."""
        return self.test_payments.strip().lower() == "on"

    @property
    def meta_graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.meta_graph_version}"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_serverless(self) -> bool:
        """Running as a Vercel Function rather than a long-lived server."""
        return self.vercel.strip() == "1"

    def missing_credentials(self) -> list[str]:
        """Names of credentials that are still blank or obviously unfilled.

        Used by the /health/readiness probe and by `scripts/check_config.py` so
        a half-configured deployment fails loudly instead of at 3am on a live
        customer's payment.
        """
        required = {
            "GALLABOX_API_KEY": self.gallabox_api_key,
            "GALLABOX_API_SECRET": self.gallabox_api_secret,
            "GALLABOX_CHANNEL_ID": self.gallabox_channel_id,
            "GALLABOX_WEBHOOK_TOKEN": self.gallabox_webhook_token,
            # META_CATALOG_ID / META_SYSTEM_USER_TOKEN are optional: the menu
            # is served from this database, not the Meta catalogue.
            "OPS_API_KEY": self.ops_api_key,
            "ADMIN_SESSION_SECRET": self.admin_session_secret,
            "SETTINGS_ENCRYPTION_KEY": self.settings_encryption_key,
        }
        # Each business needs the keys of the services it actually uses.
        if self.zoho_required:
            required |= {"ZOHO_CLIENT_ID": self.zoho_client_id,
                         "ZOHO_CLIENT_SECRET": self.zoho_client_secret,
                         "ZOHO_REFRESH_TOKEN": self.zoho_refresh_token}
        if self.delivery_provider == "uber":
            required |= {"UBER_CUSTOMER_ID": self.uber_customer_id,
                         "UBER_CLIENT_ID": self.uber_client_id,
                         "UBER_CLIENT_SECRET": self.uber_client_secret}
        if self.payment_provider == "stripe":
            required |= {"STRIPE_SECRET_KEY": self.stripe_secret_key,
                         "STRIPE_WEBHOOK_SECRET": self.stripe_webhook_secret}
        else:
            required |= {"RAZORPAY_KEY_ID": self.razorpay_key_id,
                         "RAZORPAY_KEY_SECRET": self.razorpay_key_secret,
                         "RAZORPAY_WEBHOOK_SECRET": self.razorpay_webhook_secret}
        return sorted(name for name, value in required.items() if is_unset(value))


class _OwnDotEnv(DotEnvSettingsSource):
    """The .env file, prefixed variables only.

    pydantic-settings also hands over any .env line it did not match to a
    field, by its bare name - so GALLABOX_CHANNEL_ID (Shero US's number) would
    become DST India's channel. Only the matched, DST_-prefixed values are
    kept here."""

    def __call__(self):
        return EnvSettingsSource.__call__(self)


class DstSettings(Settings):
    """DST India's settings: every DST_ variable in .env, India's defaults
    below, and - for what both businesses share - Shero US's values.

    Its own (never taken from Shero US): the database, the WhatsApp channel and
    number, payments, delivery, Zoho, templates, money and address terms,
    business rules and everything set on its admin pages. Shared unless a DST_
    variable says otherwise: SHARED_FIELDS - the Gallabox account, admin
    sign-in, hosting and Google Maps.
    """

    model_config = SettingsConfigDict(
        env_prefix="DST_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", case_sensitive=False,
    )

    business_name: str = "DST India"
    currency: str = "INR"
    currency_symbol: str = "₹"
    country_code: str = "IN"
    geocoder_country: str = "IN"
    phone_country_code: str = "91"
    distance_unit: Literal["mi", "km"] = "km"
    postal_code_label: str = "PIN code"
    postal_code_digits: int = Field(default=6, ge=3, le=10)
    default_timezone: str = "Asia/Kolkata"
    payment_provider: Literal["stripe", "razorpay"] = "razorpay"
    delivery_provider: Literal["uber", "own"] = "own"
    template_prefix: str = "dst_"
    order_number_prefix: str = Field(default="DST", pattern=r"^[A-Z]{2,5}$")
    stripe_currency: str = "inr"
    zoho_data_center: str = "in"
    zoho_required: bool = False
    price_required: bool = True
    straight_to_menu: bool = True
    # Customers order for now (delivered in about instant_lead_minutes) or a
    # later time, like any food delivery app; not a day ahead as in the US.
    instant_orders: bool = True
    # "Your order is being prepared" right after paying.
    send_preparing_message: bool = True
    # GST on restaurant food; confirm with the client.
    tax_percent: float = Field(default=5.0, ge=0, le=100)
    # The bot answers nobody on the DST number until it is switched on from
    # DST India > Bot replies: the number already runs a Gallabox bot flow,
    # and two bots answering one customer is worse than one.
    bot_reply_mode: str = Field(default="allowlist",
                                validation_alias="DST_ADMIN_PAGE_ONLY_BOT_REPLY_MODE")
    bot_allowed_numbers: str = Field(default="",
                                     validation_alias="DST_ADMIN_PAGE_ONLY_BOT_ALLOWED_NUMBERS")
    bot_reply_trigger: str = Field(default="",
                                   validation_alias="DST_ADMIN_PAGE_ONLY_BOT_REPLY_TRIGGER")
    bot_trigger_keywords: str = Field(default="",
                                      validation_alias="DST_ADMIN_PAGE_ONLY_BOT_TRIGGER_KEYWORDS")
    bot_welcome_menu: str = Field(default="",
                                  validation_alias="DST_ADMIN_PAGE_ONLY_BOT_WELCOME_MENU")
    ask_customer_email: str = Field(default="",
                                    validation_alias="DST_ADMIN_PAGE_ONLY_ASK_CUSTOMER_EMAIL")
    test_payments: str = Field(default="", validation_alias="DST_ADMIN_PAGE_ONLY_TEST_PAYMENTS")
    order_alert_numbers: str = Field(default="",
                                     validation_alias="DST_ADMIN_PAGE_ONLY_ORDER_ALERT_NUMBERS")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings, env_settings, _OwnDotEnv(settings_cls), file_secret_settings)


# What a second business takes from Shero US unless its own DST_ variable is
# set: one Gallabox account, one admin sign-in, one server.
SHARED_FIELDS: tuple[str, ...] = (
    "app_env", "log_level", "enable_scheduler", "vercel", "cron_secret",
    "blob_read_write_token", "media_dir",
    "gallabox_api_key", "gallabox_api_secret", "gallabox_account_id",
    "gallabox_base_url", "gallabox_webhook_token",
    "ops_api_key", "admin_session_secret", "admin_session_hours",
    "settings_encryption_key", "admin_bootstrap_email", "admin_bootstrap_password",
    "meta_graph_version", "google_maps_api_key", "google_maps_browser_key",
    "distance_mode", "order_mode", "outlet_source", "slot_source",
    "payment_link_ttl_minutes", "payment_reminder_minutes",
    "send_payment_reminder", "send_delivery_updates",
    "slot_hold_minutes", "slot_min_lead_hours", "kitchen_alert_hour",
    "feedback_delay_minutes",
)

SETTINGS_CLASSES: dict[str, type[Settings]] = {"us": Settings, "dst": DstSettings}


def tenant_database_url(shared_url: str, key: str) -> str:
    """A second business's database when none is given: for SQLite, a file
    beside Shero's (data/shero.db -> data/shero_dst.db). Any other database
    has to be created, so there is no guess: DST_DATABASE_URL must be set."""
    url = (shared_url or "").strip()
    if not url.startswith("sqlite") or ":memory:" in url:
        return ""
    base, _, query = url.partition("?")
    stem, dot, ext = base.rpartition(".")
    if not dot or "/" in ext:
        return f"{base}_{key}" + (f"?{query}" if query else "")
    return f"{stem}_{key}.{ext}" + (f"?{query}" if query else "")


def under_prefix(url: str, prefix: str) -> str:
    """`url` moved under `prefix` on the same site:
    https://x.com/pay + /dst -> https://x.com/dst/pay."""
    url = (url or "").rstrip("/")
    scheme, sep, rest = url.partition("://")
    if not sep:
        return f"{prefix}{url}"
    host, slash, path = rest.partition("/")
    return f"{scheme}://{host}{prefix}" + (f"/{path}" if slash else "")


def build_settings(tenant) -> Settings:
    """A fresh settings object for `tenant`, read from the environment."""
    own = SETTINGS_CLASSES.get(tenant.key, Settings)()
    if tenant.is_default:
        return own
    shared = Settings()
    for name in SHARED_FIELDS:
        if name not in own.model_fields_set:
            setattr(own, name, getattr(shared, name))
    # Its pages and links live under its own prefix on the same site.
    for name in ("public_base_url", "pay_redirect_base_url"):
        if name not in own.model_fields_set:
            setattr(own, name, under_prefix(getattr(shared, name), tenant.url_prefix))
    # Unset or a blank line (DST_DATABASE_URL= as .env.example has it): the
    # automatic file beside Shero US's SQLite database.
    if "database_url" not in own.model_fields_set or not own.database_url.strip():
        own.database_url = tenant_database_url(shared.database_url, tenant.key)
    return own


_instances: dict[str, Settings] = {}


def settings_for(tenant) -> Settings:
    """The live settings of `tenant` (admin-page overrides applied on top)."""
    instance = _instances.get(tenant.key)
    if instance is None:
        instance = _instances[tenant.key] = build_settings(tenant)
    return instance


def current_settings() -> Settings:
    """The live settings of the business being served right now."""
    from app.core import tenants

    return settings_for(tenants.current())


def get_settings() -> Settings:
    return current_settings()


class _CurrentSettings:
    """`settings`, as every module imports it: whichever business is being
    served (app/core/tenants.py). Reading, setting and monkeypatching an
    attribute all go to that business's own Settings object."""

    __slots__ = ()

    def __getattr__(self, name: str):
        return getattr(current_settings(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(current_settings(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(current_settings(), name)

    def __repr__(self) -> str:
        return f"<settings of {current_settings().business_name}>"


settings = _CurrentSettings()
