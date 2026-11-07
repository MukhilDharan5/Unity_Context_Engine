"""Versioned read-only context on the user's D-Bus session bus."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json

from dbus_next import DBusError, ErrorType, NameFlag, RequestNameReply
from dbus_next.service import ServiceInterface, method, signal

from .media import new_bus
from .model import API_BUS_NAME
from .workload import predict


API_VERSION = 1
API_PATH = "/" + API_BUS_NAME.replace(".", "/")


class ContextAPI(ServiceInterface):
    def __init__(self, snapshot: dict):
        super().__init__(API_BUS_NAME)
        self._snapshot = snapshot
        self._revision = 1
        self._json = None
        self._data = None
        self.listeners = set()

    def publish(self, snapshot: dict) -> None:
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self._revision += 1
            self._json = None
            self._data = None
            self.SnapshotUpdated(self._snapshot_json())
            self.SnapshotChanged(self._revision)
            for listener in tuple(self.listeners):
                listener(self._values())

    def _values(self) -> dict:
        if self._data is None:
            workloads = [asdict(item) for item in predict(self._snapshot)]
            names = [item["name"] for item in workloads]
            media_live = self._snapshot["sources"].get("media", {}).get("status") == "live"
            self._data = self._snapshot | {
                "workloads": workloads,
                "workload": names[0] if names else None,
                "workload_names": names,
                "media_playing": any(p["playback"] == "Playing" for p in self._snapshot["media"]) if media_live else None,
            }
        return self._data

    def _encode(self, data: dict) -> str:
        return json.dumps({"api_version": API_VERSION, "revision": self._revision, "data": data},
                          separators=(",", ":"))

    @method()
    def ListFields(self) -> 'as':
        return sorted(self._values())

    def _snapshot_json(self) -> str:
        if self._json is None:
            self._json = self._encode(self._values())
        return self._json

    @method()
    def GetSnapshot(self) -> 's':
        return self._snapshot_json()

    @method()
    def GetFields(self, fields: 'as') -> 's':
        valid = self._values()
        if len(fields) > len(valid) or any(key not in valid for key in fields):
            raise DBusError(ErrorType.INVALID_ARGS, "Pass supported field names from ListFields")
        return self._encode({key: valid[key] for key in fields})

    @signal()
    def SnapshotUpdated(self, snapshot) -> 's':
        """Push the complete versioned snapshot to change subscribers."""
        return snapshot

    @signal()
    def SnapshotChanged(self, revision) -> 't':
        return revision


@asynccontextmanager
async def export(snapshot: dict):
    bus = None
    try:
        try:
            bus = new_bus()
            async with asyncio.timeout(3):
                await bus.connect()
                service = ContextAPI(snapshot)
                bus.export(API_PATH, service)
                result = await bus.request_name(API_BUS_NAME, NameFlag.DO_NOT_QUEUE)
            if result != RequestNameReply.PRIMARY_OWNER:
                raise OSError("the context API is already running")
        except Exception as error:
            raise OSError(f"Could not start the context API: {error}") from error
        yield bus, service
    finally:
        if bus is not None:
            bus.disconnect()
            bus._finalize()
