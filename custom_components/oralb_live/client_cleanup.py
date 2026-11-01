"""Keep BLE backend cleanup alive independently of connection tasks."""

from __future__ import annotations

import asyncio
import logging

from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class ClientCleanup:
    """Serialize reuse behind cleanup, even when the caller is cancelled.

    All methods run on Home Assistant's event loop. A remote disconnect is
    not sufficient to close the private bus on recent bleak BlueZ backends.
    Do not gate disconnect() on is_connected or cancel it with a short outer
    timeout: that can prevent bleak from reaching its bus-closing step.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._tasks: dict[BleakClientWithServiceCache, asyncio.Task] = {}

    def start(self, client: BleakClientWithServiceCache) -> asyncio.Task:
        """Retain one cleanup task per client until it finishes."""
        if client in self._tasks:
            return self._tasks[client]

        async def cleanup() -> None:
            try:
                await client.disconnect()
            except Exception as err:
                _LOGGER.debug("BLE client cleanup failed: %s", err)

        task = self._hass.async_create_task(cleanup())
        self._tasks[client] = task
        # HA may start tasks eagerly, including completing disconnect() before
        # async_create_task returns. Remove only after the task is registered.
        def finished(done: asyncio.Task) -> None:
            if self._tasks.get(client) is done:
                self._tasks.pop(client)

        task.add_done_callback(finished)
        return task

    async def wait(self) -> None:
        """Wait for retained cleanups without propagating caller cancellation."""
        while self._tasks:
            # Done callbacks may still be queued. Awaiting an already finished
            # gather need not yield, so prune synchronously to avoid spinning.
            for client, task in tuple(self._tasks.items()):
                if task.done():
                    self._tasks.pop(client)
            if self._tasks:
                await asyncio.shield(asyncio.gather(*tuple(self._tasks.values())))
