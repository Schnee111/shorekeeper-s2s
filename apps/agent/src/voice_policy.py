"""Voice Notification Policy (P1 — Conversational UX).

Menerapkan state machine percakapan suara dan policy gate:
- VOICE_IDLE: aman menyuarakan notifikasi.
- USER_SPEAKING: tahan/antrekan notifikasi (jangan memotong user).
- MODEL_SPEAKING: tahan/antrekan notifikasi kecuali critical failure.
- BATCHING/COALESCE: gabungkan beberapa task selesai menjadi 1 kalimat alami.
"""

import time
from dataclasses import dataclass
from enum import Enum


class VoiceConversationState(str, Enum):
    VOICE_IDLE = "voice_idle"
    USER_SPEAKING = "user_speaking"
    MODEL_SPEAKING = "model_speaking"


@dataclass
class PendingNotification:
    task_id: str
    status: str
    summary: str
    priority: str = "normal"  # "normal" | "critical"
    created_at: float = 0.0


class VoiceNotificationPolicy:
    def __init__(self):
        self.state = VoiceConversationState.VOICE_IDLE
        self.queue: list[PendingNotification] = []

    def set_state(self, new_state: VoiceConversationState):
        self.state = new_state

    def enqueue(self, notif: PendingNotification):
        if not notif.created_at:
            notif.created_at = time.time()
        self.queue.append(notif)

    def can_deliver_now(self) -> bool:
        if self.state == VoiceConversationState.VOICE_IDLE:
            return True
        # Critical failure boleh menginterupsi model speaking saat aman
        if self.state == VoiceConversationState.MODEL_SPEAKING:
            return any(n.priority == "critical" for n in self.queue)
        # Jangan pernah menginterupsi user saat sedang bicara
        return False

    def drain_deliverable(self) -> list[PendingNotification]:
        if not self.can_deliver_now():
            return []
        items = list(self.queue)
        self.queue.clear()
        return items

    @staticmethod
    def coalesce_to_speech(notifications: list[PendingNotification]) -> str:
        if not notifications:
            return ""
        if len(notifications) == 1:
            n = notifications[0]
            if n.status == "done":
                return f"Task {n.task_id} selesai. {n.summary}"
            return f"Task {n.task_id} {n.status}. {n.summary}"

        done_count = sum(1 for n in notifications if n.status == "done")
        failed_count = sum(
            1 for n in notifications if n.status in ("failed", "cancelled")
        )

        parts = []
        if done_count > 0:
            parts.append(f"{done_count} tugas berhasil selesai")
        if failed_count > 0:
            parts.append(f"{failed_count} tugas mengalami kendala")

        return f"Update status: {', dan '.join(parts)}."
