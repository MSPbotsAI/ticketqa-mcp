from .._json import error_envelope

NO_TOKEN = error_envelope(
    "not_configured",
    "No TicketQA credentials. Send the X-MSP-Token, X-MSP-Tenant-Id, and X-MSP-Host headers.",
    False,
)

SCHEMA_VERSION = "2.0"
