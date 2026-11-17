"""Constants for Render Monitor."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "render_monitor"

# Where a Render account owner creates an API key (Account Settings > API Keys).
API_KEYS_URL: Final = "https://dashboard.render.com/u/settings#api-keys"

CONF_API_KEY: Final = "api_key"
CONF_OWNER_ID: Final = "owner_id"
CONF_OWNER_NAME: Final = "owner_name"
# Selected service ids; an empty list means "every service in the workspace", new ones included.
CONF_SERVICES: Final = "services"
CONF_SCAN_INTERVAL: Final = "scan_interval_minutes"
CONF_METRICS_INTERVAL: Final = "metrics_interval_minutes"

DEFAULT_SCAN_INTERVAL: Final = 5
DEFAULT_METRICS_INTERVAL: Final = 15
MIN_SCAN_INTERVAL: Final = 2  # with 400 GETs a minute at Render, every minute is too often for big workspaces
MIN_METRICS_INTERVAL: Final = 5

# Render instance types, smallest first. Prices are USD per month per instance.
# PRICES ON 2026-11-16 - check https://render.com/pricing before trusting them.
PRICES_DATE: Final = "2026-11-16"
PRICING_URL: Final = "https://render.com/pricing"
# "label" is what the advice shows: the name on Render's pricing page, or CPU / RAM where
# the API id of the type is not confirmed (2c-8g is a guess from the API's size-based ids).
INSTANCE_TYPES: Final = (
    # plan id, label, CPU, RAM in GB, USD per month
    {"plan": "free", "label": "Free", "cpu": 0.1, "memory_gb": 0.5, "price": 0.0},
    {"plan": "starter", "label": "Starter", "cpu": 0.5, "memory_gb": 0.5, "price": 7.0},
    {"plan": "standard", "label": "Standard", "cpu": 1.0, "memory_gb": 2.0, "price": 25.0},
    {"plan": "pro", "label": "Pro", "cpu": 2.0, "memory_gb": 4.0, "price": 85.0},
    {"plan": "2c-8g", "label": "2 CPU / 8 GB", "cpu": 2.0, "memory_gb": 8.0, "price": 135.0},
    {"plan": "pro_plus", "label": "Pro Plus", "cpu": 4.0, "memory_gb": 8.0, "price": 175.0},
    {"plan": "pro_max", "label": "Pro Max", "cpu": 4.0, "memory_gb": 16.0, "price": 225.0},
    {"plan": "pro_ultra", "label": "Pro Ultra", "cpu": 8.0, "memory_gb": 32.0, "price": 450.0},
)
# Persistent disk, USD per GB per month.
DISK_PRICE_PER_GB: Final = 0.25

# Service types Render reports (GET /services "type").
SERVICE_TYPES: Final = (
    "web_service",
    "private_service",
    "background_worker",
    "cron_job",
    "static_site",
)

# ---------------------------------------------------------------- coordinator / advice

# Log lines the apps write (GET /logs "text" filter). "slow:" marks a slow request,
# "import" a data import job; both are searched as plain text by Render.
SLOW_LOG_TEXT: Final = "slow:"
IMPORT_LOG_TEXT: Final = "import"

# Deploy states (GET /services/{id}/deploys "status").
DEPLOY_FAILED: Final = frozenset({"build_failed", "update_failed", "pre_deploy_failed"})
# Deploys that got past the build and replaced the running instance (or are doing it now).
DEPLOY_RESTARTED: Final = frozenset({"live", "deactivated", "update_failed", "update_in_progress"})
DEPLOY_RUNNING: Final = frozenset({
    "created", "queued", "build_in_progress", "update_in_progress", "pre_deploy_in_progress",
})

# Service states shown by the status sensor.
STATUS_OPTIONS: Final = ["live", "deploying", "deploy_failed", "suspended", "not_deployed"]

# Advice thresholds.
MEMORY_HIGH_PCT: Final = 80.0
DISK_HIGH_PCT: Final = 80.0
DISK_CRITICAL_PCT: Final = 95.0
MANY_DEPLOYS_PER_DAY: Final = 5
LATENCY_SPIKE_MS: Final = 1000.0  # a p95 point is a spike when >= this AND >= 2x the day's median
SLOW_SPIKE_PER_HOUR: Final = 5  # an hour with this many "slow:" lines AND >= 3x the median hour
CAUSE_WINDOW_MINUTES: Final = 15  # an import/deploy this close to a spike is named as its cause

SEVERITIES: Final = ("critical", "warning", "info")
