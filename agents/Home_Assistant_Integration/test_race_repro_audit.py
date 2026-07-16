"""Audit repro: submit_upload during an in-flight drain loses the new image."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opendisplay import RefreshMode
import pytest

from custom_components.opendisplay import delivery as delivery_mod
from custom_components.opendisplay.delivery import DeliveryManager
from custom_components.opendisplay.sleep import SleepProfile

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _profile():
    return SleepProfile.create(
        sleep_mode="on",
        power_mode=1,
        sleep_timeout_ms=0,
        deep_sleep_time_seconds=300,
        missed_cycles=3,
        queue_timeout_hours=24,
    )


def _make_env():
    coordinator = MagicMock()
    coordinator.data = SimpleNamespace(last_seen=None)
    coordinator.async_subscribe_device_seen = MagicMock(return_value=MagicMock())
    runtime = SimpleNamespace(
        coordinator=coordinator,
        sleep_profile=_profile(),
        device_config=MagicMock(),
        ble_lock=asyncio.Lock(),
        config_resync_pending=False,
        firmware=None,
        is_flex=False,
    )
    entry = MagicMock()
    entry.unique_id = ADDRESS
    entry.runtime_data = runtime
    entry.data = {}
    hass = MagicMock()
    return hass, entry


def _submit(mgr, tag):
    return mgr.submit_upload(
        prepared=(tag, None, object()),
        refresh_mode=RefreshMode.FULL,
        partial_state=MagicMock(),
        use_measured_palettes=False,
        preview_jpeg=b"jpeg",
        device_id=tag.decode(),
    )


@pytest.mark.asyncio
async def test_submit_during_inflight_drain_keeps_new_upload():
    hass, entry = _make_env()

    upload_started = asyncio.Event()
    release_upload = asyncio.Event()

    device = MagicMock()

    async def _slow_upload(prepared, **kwargs):
        upload_started.set()
        await release_upload.wait()

    device.upload_prepared_image = AsyncMock(side_effect=_slow_upload)

    class _Ctx:
        async def __aenter__(self):
            return device

        async def __aexit__(self, *exc):
            return False

    with (
        patch.object(delivery_mod, "async_call_later", return_value=MagicMock()),
        patch.object(delivery_mod, "async_dispatcher_send"),
        patch.object(delivery_mod, "async_ble_device_from_address", return_value=MagicMock()),
        patch.object(delivery_mod, "OpenDisplayDevice", lambda **kw: _Ctx()),
    ):
        mgr = DeliveryManager(hass, entry)
        _submit(mgr, b"old-image")

        # Wake: the drain starts delivering the OLD image.
        drain = asyncio.get_event_loop().create_task(mgr._deliver())
        await upload_started.wait()

        # While the old image is mid-transfer, the user queues a NEW image.
        _submit(mgr, b"new-image")
        assert mgr.state.pending is True

        # The old delivery completes.
        release_upload.set()
        await drain

        # The NEW image must still be queued for the next wake.
        assert mgr.state.pending is True, (
            "new upload silently dropped by completed drain of the old upload"
        )
