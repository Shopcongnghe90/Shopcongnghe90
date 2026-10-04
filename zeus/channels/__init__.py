"""Kênh ngoài (owner D): adapter verify+normalize, router /hooks/*, outbound qua typed action.

Facebook cá nhân: KHÔNG có adapter — không có API chính thức, điều khoản cấm tự động hoá (ADR-008).
"""

from zeus.channels.router import ChannelsContext, router

__all__ = ["ChannelsContext", "router"]
