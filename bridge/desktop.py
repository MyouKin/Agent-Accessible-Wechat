import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from .models import QuoteTarget, Snapshot, message_key


class DesktopError(RuntimeError):
    pass


async def command(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=3)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode:
        raise DesktopError(f"{arguments[0]} returned {process.returncode}")
    return output.decode(errors="replace").strip()


async def vnc_view_only() -> bool:
    answer = await command("x11vnc", "-sync", "-Q", "viewonly")
    match = re.search(r"(?:^|[=,\s])viewonly:([01])\b", answer)
    if match is None:
        raise DesktopError("Could not verify whether noVNC input is paused")
    return match.group(1) == "1"


@asynccontextmanager
async def pause_vnc_input():
    was_view_only = await vnc_view_only()
    try:
        if not was_view_only:
            await command("x11vnc", "-sync", "-R", "viewonly")
            if not await vnc_view_only():
                raise DesktopError("Could not pause noVNC input")
        yield
    finally:
        if not was_view_only:
            await command("x11vnc", "-sync", "-R", "noviewonly")


@asynccontextmanager
async def clipboard_text(text: str):
    process = await asyncio.create_subprocess_exec(
        "xclip", "-quiet", "-selection", "clipboard", "-in",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        process.stdin.write(text.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        # Foreground xclip reports when it starts serving selection requests.
        await asyncio.wait_for(process.stderr.readuntil(b"Waiting for selection requests"), 2)
        yield
    except asyncio.IncompleteReadError as error:
        raise DesktopError("Could not own the X11 clipboard") from error
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        await process.wait()


class Target(Protocol):
    pid: int
    window_bounds: tuple[int, int, int, int]
    input_bounds: tuple[int, int, int, int]
    chat: str


class SendSource(Protocol):
    async def get_target(self) -> Target | None: ...
    async def focus_input(self) -> bool: ...
    async def read_input(self) -> str | None: ...
    async def input_focused(self) -> bool: ...
    async def read_snapshot(self, *, capture_media: bool = True) -> Snapshot: ...
    async def select_quote(self, snapshot: Snapshot, index: int, *, allow_own: bool = False) -> bool: ...
    async def quote_matches(self, text: str) -> bool: ...
    async def quote_clear(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SendWitness:
    """The snapshots around submission and the exact row index in the later snapshot."""

    before: Snapshot
    after: Snapshot
    after_index: int

    @property
    def before_keys(self) -> tuple[str, ...]:
        return tuple(map(message_key, self.before.messages))

    @property
    def after_keys(self) -> tuple[str, ...]:
        return tuple(map(message_key, self.after.messages))


@dataclass(frozen=True, slots=True)
class SendResult:
    state: str
    detail: str | None = None
    witness: SendWitness | None = None
    submission_attempted: bool = False


def accessible_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\u2029", "\n")


def visible_quote_index(snapshot: Snapshot, quote: QuoteTarget) -> int | None:
    if not quote.keys:
        return None
    keys = tuple(map(message_key, snapshot.messages))
    if keys.count(quote.keys[0]) != 1:
        return None
    index = keys.index(quote.keys[0])
    if keys[index:index + len(quote.keys)] == quote.keys:
        return index
    return None


def matches_sent_text(observed: str, reply: str, quote: QuoteTarget | None = None) -> bool:
    observed, reply = accessible_text(observed), accessible_text(reply)
    if quote is None:
        return observed == reply
    # This is the accessible label of a sent quoted reply in the verified
    # English client. Keep the entire original body in the comparison.
    prefix = reply + "\nQuote "
    suffix = "'s message: " + accessible_text(quote.text)
    return observed.startswith(prefix) and observed.endswith(suffix) and len(observed) > len(prefix) + len(suffix)


def _unique_tail_end(before: tuple[str, ...], after: tuple[str, ...]) -> int | None:
    if not before:
        return 0
    for length in range(min(len(before), len(after)), 0, -1):
        suffix = before[-length:]
        ends = [index + length for index in range(len(after) - length + 1)
                if after[index:index + length] == suffix]
        if len(ends) > 1:
            raise DesktopError("The pre-submit message anchor is ambiguous")
        if ends:
            return ends[0]
    return None


class MessageSender:
    def __init__(self, chat: str, source: SendSource, lock: asyncio.Lock, send_key: str):
        self.chat = chat
        self.source = source
        self.lock = lock
        self.send_key = send_key

    def set_target(self, chat: str) -> None:
        """Rebind under the Bridge operation lock."""
        self.chat = chat

    async def active_target(self) -> str:
        target = await self.source.get_target()
        if target is None or not self.chat or target.chat != self.chat:
            raise DesktopError("The target chat and its input could not be verified")
        window = await command("xdotool", "getactivewindow")
        pid = await command("xdotool", "getwindowpid", window)
        if not pid.isdecimal() or int(pid) != target.pid:
            raise DesktopError("The target chat is not the active X11 window")
        geometry = await command("xdotool", "getwindowgeometry", "--shell", window)
        fields = dict(line.split("=", 1) for line in geometry.splitlines() if "=" in line)
        x, y, width, height = target.window_bounds
        left, top, input_width, input_height = target.input_bounds
        if (
            min(width, height, input_width, input_height) <= 0
            or not (x <= left < left + input_width <= x + width)
            or not (y <= top < top + input_height <= y + height)
            or int(fields.get("X", -9999)) != x
            or int(fields.get("Y", -9999)) != y
            or int(fields.get("WIDTH", 0)) != width
            or int(fields.get("HEIGHT", 0)) != height
        ):
            raise DesktopError("The active X11 window does not match the accessible window")
        return window

    async def focus_verified_input(self, window: str) -> None:
        if not await self.source.focus_input():
            target = await self.source.get_target()
            if target is None or await self.active_target() != window:
                raise DesktopError("The chat input could not be located")
            x, y, width, height = target.input_bounds
            await command("xdotool", "mousemove", "--sync",
                          str(x + width // 2), str(y + height // 2), "click", "1")
            for _ in range(8):
                if await self.source.input_focused():
                    break
                await asyncio.sleep(0.1)
        if (
            await self.active_target() != window
            or not await self.source.input_focused()
            or await self.source.read_input() != ""
        ):
            raise DesktopError("The verified empty input could not be focused")

    async def input_matches(self, text: str) -> bool:
        actual = await self.source.read_input()
        return actual is not None and accessible_text(actual) == accessible_text(text)

    async def verified_tail(self) -> Snapshot:
        snapshot = await self.source.read_snapshot(capture_media=False)
        if not snapshot.ready or not snapshot.at_tail or snapshot.chat != self.chat:
            raise DesktopError("Return to the end of the target chat before sending")
        return snapshot

    async def verify_composer(self, window: str, text: str, quote: QuoteTarget | None) -> None:
        if (await self.active_target() != window or not await self.source.input_focused()
                or not await self.input_matches(text)):
            raise DesktopError("The verified input changed before sending")
        if quote is not None and not await self.source.quote_matches(quote.text):
            raise DesktopError("The quote changed before sending")

    async def send(
        self, text: str, *, quote: QuoteTarget | None = None,
        before_submit: Callable[[Snapshot], Awaitable[None]] | None = None,
    ) -> SendResult:
        filled = False
        submission_attempted = False
        confirmed = None
        try:
            async with self.lock, pause_vnc_input():
                window = await self.active_target()
                before = await self.verified_tail()
                if await self.source.read_input() != "":
                    raise DesktopError("The chat input is occupied or unreadable")
                check_clear = getattr(self.source, "quote_clear", None)
                if check_clear is not None and not await check_clear():
                    raise DesktopError("The composer already contains an active quote")
                if quote is not None:
                    index = visible_quote_index(before, quote)
                    if index is None or not await self.source.select_quote(before, index, allow_own=quote.allow_own):
                        raise DesktopError("The exact quote target could not be selected")
                    if not await self.source.quote_matches(quote.text):
                        raise DesktopError("The quoted text could not be verified")
                await self.focus_verified_input(window)
                async with clipboard_text(text):
                    filled = True
                    await command("xdotool", "key", "--clearmodifiers", "ctrl+v")
                    for _ in range(10):
                        if await self.input_matches(text):
                            break
                        await asyncio.sleep(0.1)
                    else:
                        raise DesktopError("The input did not contain the exact draft after paste")
                await self.verify_composer(window, text, quote)
                before = await self.verified_tail()
                prior_keys = tuple(map(message_key, before.messages))
                if before_submit is not None:
                    try:
                        await before_submit(before)
                    except Exception as error:
                        raise DesktopError("Could not persist the send checkpoint") from error
                    current = await self.verified_tail()
                    if tuple(map(message_key, current.messages)) != prior_keys:
                        raise DesktopError("The message sequence changed before submission")
                await self.verify_composer(window, text, quote)
                submission_attempted = True
                await command("xdotool", "key", "--clearmodifiers", self.send_key)
                confirmation_view = None
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    if await self.active_target() != window:
                        break
                    after = await self.source.read_snapshot(capture_media=False)
                    if not after.ready or not after.at_tail or after.chat != self.chat:
                        break
                    after_keys = tuple(map(message_key, after.messages))
                    end = _unique_tail_end(prior_keys, after_keys)
                    if end is None or await self.source.read_input() != "":
                        confirmation_view = None
                        continue
                    matching = [
                        index for index, message in enumerate(after.messages[end:], start=end)
                        if message.is_self is not False and message.text is not None
                        and matches_sent_text(message.text, text, quote)
                    ]
                    if len(matching) > 1:
                        return SendResult("uncertain", "Several new rows match the submitted draft",
                                          submission_attempted=True)
                    if len(matching) == 1 and (quote is None or await self.source.quote_clear()):
                        current_view = (after_keys, matching[0])
                        if confirmation_view != current_view:
                            confirmation_view = current_view
                            continue
                        detail = ("Composer cleared and one matching new row appeared" if quote is None
                                  else "Verified native quote and matching sent reply")
                        confirmed = SendResult("sent", detail, SendWitness(before, after, matching[0]), True)
                        return confirmed
                    confirmation_view = None
                return SendResult("uncertain", "The cleared input and a matching new message could not both be verified",
                                  submission_attempted=True)
        except (RuntimeError, OSError, TimeoutError, ValueError) as error:
            if confirmed is not None:
                return confirmed
            return SendResult("uncertain" if filled else "failed",
                              str(error) or type(error).__name__, submission_attempted=submission_attempted)
