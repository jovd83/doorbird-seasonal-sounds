"""Constants for the DoorBird Seasonal Sounds integration."""

DOMAIN = "doorbird_seasonal"

CONF_DEVICES = "devices"
CONF_SCHEDULES = "schedules"
CONF_MP3_DIR = "mp3_dir"
CONF_DEFAULT_MP3 = "default_mp3"
CONF_DAILY_RUN_TIME = "daily_run_time"

CONF_FROM = "from"
CONF_TO = "to"
CONF_MP3 = "mp3"
CONF_PRIORITY = "priority"
CONF_YEAR = "year"
CONF_START_YEAR = "start_year"
CONF_END_YEAR = "end_year"
# Time-of-day window, matching the Docker app. Omitting both means all day.
CONF_START_TIME = "start_time"
CONF_END_TIME = "end_time"

DEFAULT_MP3_DIR = "/config/doorbird_seasonal/mp3"
DEFAULT_DAILY_RUN_TIME = "03:15"

SIGNAL_RECONCILED = f"{DOMAIN}_reconciled"

# Service names
SERVICE_APPLY_NOW = "apply_now"
SERVICE_SET_BUTTON_SOUND = "set_button_sound"
SERVICE_ACTIVATE_BUILTIN = "activate_builtin"
SERVICE_TEST_CONNECTION = "test_connection"

API_BASE = "https://api.doorbird.io/"

ATTR_FORCE = "force"
ATTR_MP3 = "mp3"
ATTR_DEVICES = "devices"
ATTR_SOUND = "sound"
