from __future__ import annotations

DOMAIN = "unite_evcc_bridge"

CONF_UNIT_ID = "unit_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_POLL_INTERVAL = "poll_interval"
CONF_MAX_CURRENT = "max_current"
CONF_FAILSAFE_CURRENT = "failsafe_current"
CONF_FAILSAFE_TIMEOUT = "failsafe_timeout"
CONF_PHASE_RECOVERY_ENABLED = "phase_recovery_enabled"
CONF_PHASE_RECOVERY_OBSERVE = "phase_recovery_observe"
CONF_PHASE_RECOVERY_DWELL = "phase_recovery_dwell"
CONF_REST_ENABLED = "rest_enabled"
CONF_REST_USERNAME = "rest_username"
CONF_REST_PASSWORD = "rest_password"

DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 255
DEFAULT_POLL_INTERVAL = 10
DEFAULT_SCAN_INTERVAL = DEFAULT_POLL_INTERVAL
MIN_POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 60
DEFAULT_MAX_CURRENT = 16
DEFAULT_RESUME_CURRENT = 6
DEFAULT_FAILSAFE_CURRENT_A = 6
DEFAULT_FAILSAFE_TIMEOUT_S = 30
DEFAULT_PHASE_RECOVERY_ENABLED = False
DEFAULT_PHASE_RECOVERY_OBSERVE_S = 60
DEFAULT_PHASE_RECOVERY_DWELL_S = 121
DEFAULT_REST_ENABLED = False
DEFAULT_REST_USERNAME = "admin"
REST_RESTART_COOLDOWN_S = 300
PHASE_RESTORE_COOLDOWN_S = 60  # blocks re-press while the 0->1 toggle runs

# How the wallbox is physically wired. Register 404 alone cannot tell a genuine
# 1-phase installation apart from a 3-phase charger stuck at 1-phase, so the
# phase-config restore is gated on this explicit setting.
CONF_GRID_PHASES = "grid_phases"
GRID_PHASES_1 = "1"
GRID_PHASES_3 = "3"
GRID_PHASES = (GRID_PHASES_1, GRID_PHASES_3)

# Automatically re-sync a stuck 1-phase installation config while the charger is
# idle. Opt-in: it writes an installation setting over the charger's web UI.
CONF_PHASE_RESTORE_ON_UNPLUG = "phase_restore_on_unplug"
DEFAULT_PHASE_RESTORE_ON_UNPLUG = False
# Retry pacing: the "idle and stuck" condition stays true until it is fixed, so
# without a floor we would hammer the web UI every poll.
PHASE_RESTORE_RETRY_S = 900          # 15 min between attempts
PHASE_RESTORE_MAX_ATTEMPTS = 3
REST_TIMEOUT_S = 15
HEARTBEAT_ALIVE_VALUE = 1
