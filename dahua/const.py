"""Protocol constants for the Dahua DHIP (DVRIP/RPC2) wire format.

All values reconstructed from device firmware and verified against a live
SD-2N-4G camera (Rostelecom-branded, firmware 30.13...R).
"""

from __future__ import annotations

# -- wire framing -----------------------------------------------------------
DHIP_MAGIC = 0x50494844            # b"DHIP"
HEADER_SIZE = 32
HEADER_FMT = "<IIIIIIII"           # 8 x u32, little-endian
#  off  field
#    0  size/headFlag = 0x20 (constant header length)
#    4  magic = "DHIP"
#    8  sessionID      (0 before login)
#   12  requestID      (mirrors the JSON "id")
#   16  packageLength  (= messageLength + dataLength)
#   20  packageIndex   (fragment index)
#   24  messageLength  (length of the JSON text)
#   28  dataLength     (length of trailing binary; 0 for pure-JSON)

DEFAULT_PORT = 5000                # DHIP/RPC2 listener (NOT legacy 37777)
DEFAULT_TIMEOUT = 10.0
DEFAULT_KEEPALIVE = 60             # seconds, fallback when login omits an interval

# -- RPC method names -------------------------------------------------------
# Centralised so the high-level helpers and the CLI share one source of truth.
LOGIN = "global.login"
LOGOUT = "global.logout"
KEEPALIVE = "global.keepAlive"
GET_TIME = "global.getCurrentTime"
SET_TIME = "global.setCurrentTime"

GET_CONFIG = "configManager.getConfig"
SET_CONFIG = "configManager.setConfig"
RESTORE_CONFIG = "configManager.restore"

GET_USERS = "userManager.getUserInfoAll"
GET_GROUPS = "userManager.getGroupInfoAll"
GET_ACTIVE_USERS = "userManager.getActiveUserInfoAll"
ADD_USER = "userManager.addUser"
MODIFY_USER = "userManager.modifyUser"
DELETE_USER = "userManager.deleteUser"
MODIFY_PASSWORD = "userManager.modifyPassword"

PTZ_GET_PRESETS = "ptz.getPresets"
PTZ_START = "ptz.start"
PTZ_STOP = "ptz.stop"

EVENT_ATTACH = "eventManager.attach"
EVENT_DETACH = "eventManager.detach"

REBOOT = "magicBox.reboot"
SHUTDOWN = "magicBox.shutdown"

# -- PTZ operation codes ----------------------------------------------------
# Dahua PTZ verbs accepted by ptz.start / ptz.stop.
PTZ_CODES = (
    "Up", "Down", "Left", "Right",
    "LeftUp", "LeftDown", "RightUp", "RightDown",
    "ZoomTele", "ZoomWide",
    "FocusNear", "FocusFar",
    "IrisLarge", "IrisSmall",
    "GotoPreset", "SetPreset", "ClearPreset",
    "PositionABS", "Position",
    "StartTour", "StopTour",
)

# -- error-code -> friendly message -----------------------------------------
# Observed and documented Dahua RPC2 error codes.
DAHUA_ERRORS = {
    268632079: "Login challenge (expected; resend with digest)",
    268632080: "Unknown user or wrong password",
    268632081: "User has been locked",
    268632082: "User is blocked",
    268632083: "User account is in use elsewhere",
    268894210: "Insufficient permissions for this method",
    285409284: "Account does not exist",
    285409285: "Account already exists",
    285409296: "Group does not exist",
    405: "Method not allowed (wrong namespace or unsupported on this device)",
    403: "Forbidden",
}


def error_message(code: int | None, fallback: str = "RPC error") -> str:
    """Map a Dahua error code to a friendly string, falling back to *fallback*."""
    if code is None:
        return fallback
    return DAHUA_ERRORS.get(code, fallback)
