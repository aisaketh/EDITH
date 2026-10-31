"""Delivers due reminders to the owner through the chat channel."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from ..broker.gateway import ProviderError
from ..scheduler.reminders import DeliveryError, Reminder


def late_note(late_by: timedelta) -> str:
    minutes = int(late_by.total_seconds() // 60)
    if minutes < 5:
        return ""
    if minutes < 120:
        return f" (due {minutes} min ago)"
    if minutes < 48 * 60:
        return f" (due {minutes // 60} h ago)"
    return f" (due {minutes // 1440} days ago)"


class ChatNotifier:
    def __init__(self, channel, owner_chat_id_getter):
        self.channel = channel
        self._owner = owner_chat_id_getter

    def send(self, reminder: Reminder, late_by: timedelta) -> None:
        chat_id: Optional[int] = self._owner()
        if chat_id is None:
            raise DeliveryError("not_paired")
        text = f"Reminder: {reminder.text}{late_note(late_by)}"
        try:
            if self.channel.send(text, source="reminder", chat_id=chat_id):
                return
            # The guard refused the content: send a content-free nudge instead, never drop the reminder.
            generic = f"Reminder #{reminder.id} is due. Send /show {reminder.id} to read it."
            if self.channel.send(generic, source="system", chat_id=chat_id):
                return
        except ProviderError:
            pass
        raise DeliveryError("send_failed")
