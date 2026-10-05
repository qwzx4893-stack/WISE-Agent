"""Unified channel layer for outbound notifications.

Backed by Apprise (https://github.com/caronc/apprise — BSD 2-Clause).
See :mod:`core.channels.unified` for the public API.
"""

from .unified import (
    Channel, ChannelRegistry, ChannelResult,
    get_registry, list_channels, register_channel,
    send_message, unregister_channel,
)

__all__ = [
    "Channel", "ChannelRegistry", "ChannelResult",
    "get_registry", "list_channels", "register_channel",
    "send_message", "unregister_channel",
]
