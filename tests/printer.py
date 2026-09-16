"""A scripted fake printer and patched time for the transmission tests.

``ScriptedPrinter`` stands in for the ``BleakClient`` a job connects. Each
status poll it receives consumes the next entry of its ``polls`` script, and
each print-stream chunk runs the actions listed for its index in ``chunks``.
Replies are delivered through ``VirtualClock`` timers, so no test sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import heapq
from typing import Any

from bleak.exc import BleakError

from custom_components.ypl_printer import protocol
from custom_components.ypl_printer.const import (
    NOTIFY_CHARACTERISTIC_UUID,
    WRITE_CHARACTERISTIC_UUID,
)
from custom_components.ypl_printer.transport import STATUS_REQUEST_FRAME

from .bluetooth import gatt_services

# How long a scripted printer takes to answer, unless told otherwise.
REPLY_DELAY = 0.02
# Scheduler turns a virtual wait yields before it lets time pass.
_YIELDS = 20
# Real delay before an InstantClock timer fires.
_INSTANT_REPLY = 0.001


def frame(group: int, command: int, direction: int, data: bytes = b"") -> bytes:
    """Frame one YPL-v1 payload."""
    return protocol.encode_frame(
        bytes((group, command, direction)) + len(data).to_bytes(2, "little") + data
    )


def status_reply(value: int) -> bytes:
    """Frame a ``05/0b`` integer status reply."""
    return frame(0x05, 0x0B, 0x02, bytes((0x03, 0x01, 0x00, value)))


EVENT_FRAME = frame(0x05, 0x0F, 0x03, b"\x01\x02")
MODEL_REPLY = frame(0x01, 0x04, 0x02, b"\x01\x03\x00Y50")


class VirtualClock:
    """Patched time for ``transport.clock``: waits advance a virtual ``now``.

    Before letting time pass, a wait yields to the event loop so other tasks
    can finish first. Time then jumps to the next timer or to the deadline.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.log: list[tuple[Any, ...]] = []
        self._timers: list[tuple[float, int, Callable[[], None]]] = []
        self._seq = 0

    def monotonic(self) -> float:
        return self.now

    def call_later(self, delay: float, callback: Callable[[], None]) -> None:
        heapq.heappush(self._timers, (self.now + delay, self._seq, callback))
        self._seq += 1

    async def sleep(self, delay: float) -> None:
        self.log.append(("sleep", delay))
        await self.advance(delay)

    async def advance(self, delay: float) -> None:
        """Let ``delay`` seconds pass without recording a job sleep."""
        await self._run_until(self.now + delay, None)

    async def wait_for[T](self, awaitable: Awaitable[T], timeout: float) -> T:
        task = asyncio.ensure_future(awaitable)
        try:
            await self._run_until(self.now + timeout, task)
        except BaseException:
            task.cancel()
            raise
        if task.done():
            return task.result()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise TimeoutError

    async def _run_until(
        self, deadline: float, task: asyncio.Future[Any] | None
    ) -> None:
        while True:
            for _ in range(_YIELDS):
                if task is not None and task.done():
                    return
                await asyncio.sleep(0)
            if task is not None and task.done():
                return
            if self._timers and self._timers[0][0] <= deadline:
                when, _, callback = heapq.heappop(self._timers)
                self.now = max(self.now, when)
                callback()
                continue
            self.now = max(self.now, deadline)
            return


class InstantClock:
    """Real scheduling with sleeps reduced to yields; for concurrency tests."""

    def __init__(self) -> None:
        self.log: list[tuple[Any, ...]] = []

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    def call_later(self, delay: float, callback: Callable[[], None]) -> None:
        # After the write returns, so the reply can answer its poll.
        asyncio.get_running_loop().call_later(_INSTANT_REPLY, callback)

    async def sleep(self, delay: float) -> None:
        self.log.append(("sleep", delay))
        await asyncio.sleep(0)

    async def advance(self, delay: float) -> None:
        await asyncio.sleep(0)

    async def wait_for[T](self, awaitable: Awaitable[T], timeout: float) -> T:
        return await asyncio.wait_for(awaitable, timeout)


# --- script actions ------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """Answer with a status after ``delay`` seconds."""

    status: int
    delay: float = REPLY_DELAY


@dataclass(frozen=True)
class Notify:
    """Deliver raw notifications, in order, after ``delay`` seconds."""

    notifications: tuple[bytes, ...]
    delay: float = REPLY_DELAY


@dataclass(frozen=True)
class Fail:
    """Make the write raise."""

    error: BaseException


@dataclass(frozen=True)
class Delay:
    """Make the write take ``seconds`` before it returns."""

    seconds: float


@dataclass(frozen=True)
class Drop:
    """Drop the link after ``delay`` seconds."""

    delay: float = 0.0


@dataclass(frozen=True)
class Call:
    """Run a callback, e.g. to cancel the job."""

    callback: Callable[[], object]


class _Hang:
    """Make the write never return."""


HANG = _Hang()
SILENT = None

type Action = Reply | Notify | Fail | Delay | Drop | Call | _Hang
type Step = Action | list[Action] | None


class ScriptedPrinter:
    """A fake connected ``BleakClient`` driven by a script."""

    def __init__(
        self,
        clock: VirtualClock | InstantClock,
        total_chunks: int,
        polls: Iterable[Step] = (),
        chunks: dict[int, Step] | None = None,
        services: Any = None,
    ) -> None:
        self.clock = clock
        self.total_chunks = total_chunks
        self.polls = iter(polls)
        self.chunks = chunks or {}
        self.services = gatt_services() if services is None else services
        self.is_connected = True
        self.disconnected_callback: Callable[[Any], None] | None = None
        self.start_notify_error: BaseException | None = None
        self.stop_notify_step: Step = None
        self.disconnect_step: Step = None
        self.stream: list[bytes] = []
        self.poll_count = 0
        self._callback: Callable[[Any, bytearray], None] | None = None
        self._stream_state = "before"

    @property
    def log(self) -> list[tuple[Any, ...]]:
        return self.clock.log

    # --- BleakClient surface --------------------------------------------------

    async def start_notify(self, uuid: str, callback: Callable[..., None]) -> None:
        assert uuid == NOTIFY_CHARACTERISTIC_UUID
        self.log.append(("start_notify",))
        if self.start_notify_error is not None:
            raise self.start_notify_error
        self._callback = callback

    async def stop_notify(self, uuid: str) -> None:
        assert uuid == NOTIFY_CHARACTERISTIC_UUID
        self.log.append(("stop_notify",))
        await self._run(self.stop_notify_step)
        self._callback = None

    async def disconnect(self) -> None:
        self.log.append(("disconnect",))
        await self._run(self.disconnect_step)
        self.is_connected = False

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = False
    ) -> None:
        assert uuid == WRITE_CHARACTERISTIC_UUID
        assert response is False
        if not self.is_connected:
            raise BleakError("Not connected")
        if self._stream_state == "before" and data != STATUS_REQUEST_FRAME:
            self._stream_state = "during"
        if self._stream_state == "during":
            index = len(self.stream)
            self.stream.append(data)
            self.log.append(("chunk", index))
            if len(self.stream) == self.total_chunks:
                self._stream_state = "after"
            step = self.chunks.get(index)
        else:
            self.poll_count += 1
            self.log.append(("poll",))
            step = next(self.polls, SILENT)
        await self._run(step)

    # --- script ---------------------------------------------------------------

    def notify(self, data: bytes) -> None:
        """Deliver one notification, if still subscribed."""
        if self._callback is not None:
            self._callback(None, bytearray(data))

    def drop(self) -> None:
        """Drop the link as Bleak reports it."""
        if not self.is_connected:
            return
        self.is_connected = False
        if self.disconnected_callback is not None:
            self.disconnected_callback(self)

    async def _run(self, step: Step) -> None:
        if step is None:
            return
        for action in step if isinstance(step, list) else [step]:
            await self._apply(action)

    async def _apply(self, action: Action) -> None:
        match action:
            case Reply(status, delay):
                self._later(delay, status_reply(status))
            case Notify(notifications, delay):
                for data in notifications:
                    self._later(delay, data)
            case Fail(error):
                raise error
            case Delay(seconds):
                await self.clock.advance(seconds)
            case Drop(delay):
                self.clock.call_later(delay, self.drop)
            case Call(callback):
                callback()
            case _Hang():
                await asyncio.Event().wait()

    def _later(self, delay: float, data: bytes) -> None:
        self.clock.call_later(delay, lambda: self.notify(data))
