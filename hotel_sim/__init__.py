"""Independent synthetic hotel allocation candidate. No live hotel integration."""
from .allocation import Allocator, BookingRequest, Decision, Policy, Room, RoomType

__all__ = ["Allocator", "BookingRequest", "Decision", "Policy", "Room", "RoomType"]
