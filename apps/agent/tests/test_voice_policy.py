import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from voice_policy import (
    PendingNotification,
    VoiceConversationState,
    VoiceNotificationPolicy,
)


def test_voice_policy_idle_delivery():
    policy = VoiceNotificationPolicy()
    assert policy.state == VoiceConversationState.VOICE_IDLE

    policy.enqueue(PendingNotification(task_id="t1", status="done", summary="Lint ok"))
    assert policy.can_deliver_now() is True
    items = policy.drain_deliverable()
    assert len(items) == 1
    assert policy.coalesce_to_speech(items) == "Task t1 selesai. Lint ok"


def test_voice_policy_user_speaking_gates_delivery():
    policy = VoiceNotificationPolicy()
    policy.set_state(VoiceConversationState.USER_SPEAKING)

    policy.enqueue(
        PendingNotification(task_id="t2", status="done", summary="Built package")
    )
    assert policy.can_deliver_now() is False
    assert len(policy.drain_deliverable()) == 0

    # User selesai bicara -> IDLE
    policy.set_state(VoiceConversationState.VOICE_IDLE)
    assert policy.can_deliver_now() is True
    assert len(policy.drain_deliverable()) == 1


def test_voice_policy_batching_coalesce():
    policy = VoiceNotificationPolicy()
    notifs = [
        PendingNotification(task_id="t1", status="done", summary="Task 1 done"),
        PendingNotification(task_id="t2", status="done", summary="Task 2 done"),
        PendingNotification(task_id="t3", status="failed", summary="Task 3 failed"),
    ]
    speech = policy.coalesce_to_speech(notifs)
    assert "2 tugas berhasil selesai" in speech
    assert "1 tugas mengalami kendala" in speech
