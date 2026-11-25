import asyncio
import json
import logging
from dataclasses import dataclass, replace

from .desktop_gate import AsyncDesktopGate
from .models import Message, Snapshot, SnapshotSource, message_key
from .storage import Store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SequenceMatch:
    state: str
    end: int = 0


def match_tail(previous: tuple[str, ...], current: tuple[str, ...]) -> SequenceMatch:
    """Find the longest old suffix in the new view and return its exclusive end."""
    if not previous:
        return SequenceMatch("matched")
    if previous == current:
        return SequenceMatch("matched", len(current))
    if not current:
        return SequenceMatch("older", 0)
    for length in range(min(len(previous), len(current)), 0, -1):
        anchor = previous[-length:]
        ends = [
            index + length
            for index in range(len(current) - length + 1)
            if current[index:index + length] == anchor
        ]
        if ends and (ends[0] == length or len(ends) == 1):
            return SequenceMatch("matched", ends[0])
        if ends:
            return SequenceMatch("ambiguous")
    if any(previous[index:index + len(current)] == current
           for index in range(len(previous) - len(current) + 1)):
        return SequenceMatch("older")
    return SequenceMatch("unanchored")


class Synchronizer:
    def __init__(self, source: SnapshotSource, store: Store, poll_interval: float):
        self.source = source
        self.store = store
        self.poll_interval = poll_interval
        self.anchors = store.anchors()
        self.state: str | None = None
        self.operation_lock = AsyncDesktopGate()
        self._messages_changed = asyncio.Event()
        self.generation = 0
        self.paused = False

    def target_changed(self) -> None:
        self.anchors = self.store.anchors()
        self.state = None
        self.generation += 1
        previous, self._messages_changed = self._messages_changed, asyncio.Event()
        previous.set()

    async def wait_for_messages(self, after_id: int, limit: int, timeout: float) -> list[dict]:
        clock = asyncio.get_running_loop().time
        deadline = clock() + timeout
        generation = self.generation
        while generation == self.generation:
            changed = self._messages_changed
            rows = self.store.messages(limit=limit, after_id=after_id)
            remaining = deadline - clock()
            if rows or remaining <= 0:
                return rows
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                break
        return self.store.messages(limit=limit, after_id=after_id) if generation == self.generation else []

    def report(self, state: str) -> None:
        if state != self.state:
            self.store.save_sync(state)
            self.state = state

    def _save_observations(
        self, keys: tuple[str, ...], observations: tuple[Message, ...],
        observed_keys: tuple[str, ...], start: int, *, gap: str | None = None,
        confirmed: tuple[int, dict, str | None] | None = None,
    ) -> None:
        messages = []
        for message in observations:
            source = (
                "same_account_outside_bridge" if message.is_self is True
                else "group_member" if message.is_self is False else "observed_outside_bridge"
            )
            messages.append(replace(message, author_kind="human", author_source=source))

        resolved = []
        for attempt in self.store.unverified_model_submissions():
            previous = tuple(json.loads(attempt["submission_anchors"]))
            overlap = match_tail(previous, observed_keys)
            first = max(0, overlap.end - start) if overlap.state == "matched" else 0
            possible = []
            for i in range(first, len(messages)):
                message = messages[i]
                if message.is_self is False or message.text is None:
                    continue
                text = message.text.replace("\u2029", "\n")
                if (text == attempt["text"] or (attempt["quote_message_id"] is not None
                        and text.startswith(attempt["text"] + "\nQuote "))):
                    possible.append(i)
            for i in possible:
                messages[i] = replace(
                    messages[i], author_kind="unknown", author_source="unverified_bridge_submission",
                )
            if possible:
                resolved.append(attempt["id"])

        outcome = None
        if confirmed is not None:
            index, draft, detail = confirmed
            raw = observations[index - start]
            messages[index - start] = replace(
                raw, is_self=True, author_kind=draft["author_kind"],
                author_source="confirmed_bridge_submission", outbound_draft_id=draft["id"],
                metadata={**raw.metadata, "observation_key": message_key(raw)},
            )
            outcome = (draft["id"], detail)

        self.store.save_sync(
            "tracking", anchors=keys, messages=tuple(messages), gap=gap,
            finished_draft=outcome, resolved_attempts=tuple(resolved),
        )
        self.state, self.anchors = "tracking", keys
        if messages:
            previous_event = self._messages_changed
            self._messages_changed = asyncio.Event()
            previous_event.set()

    def commit_send(self, draft: dict, result) -> None:
        witness = result.witness
        before = tuple(map(message_key, witness.before.messages))
        after = tuple(map(message_key, witness.after.messages))
        overlap = match_tail(before, after)
        index = witness.after_index
        if (not witness.before.ready or not witness.after.ready
                or witness.before.chat != self.store.chat or witness.after.chat != self.store.chat
                or not witness.before.at_tail or not witness.after.at_tail
                or overlap.state != "matched" or not overlap.end <= index < len(after)):
            raise ValueError("The confirmed send did not identify one new occurrence")
        anchor_overlap = match_tail(self.anchors if self.anchors is not None else before, after)
        continuous = anchor_overlap.state == "matched" and anchor_overlap.end <= index
        start = anchor_overlap.end if continuous else overlap.end
        self._save_observations(
            after, witness.after.messages[start:], after, start,
            gap=None if continuous else "confirmed_send_without_previous_anchor",
            confirmed=(index, draft, result.detail),
        )

    async def step(self) -> None:
        snapshot = await self.source.read_snapshot(capture_media=False)
        if not self.store.chat:
            self.report("unconfigured")
            return
        if not snapshot.ready or snapshot.chat != self.store.chat:
            self.report("waiting_for_target")
            return
        if not snapshot.at_tail:
            self.report("waiting_for_tail")
            return
        keys = tuple(map(message_key, snapshot.messages))
        if self.anchors is None:
            attempts = self.store.unverified_model_submissions()
            if attempts:
                checkpoint = tuple(json.loads(attempts[0]["submission_anchors"]))
                match = match_tail(checkpoint, keys)
                if match.state == "matched":
                    messages = await self.source.capture_messages(snapshot, range(match.end, len(keys)))
                    self._save_observations(keys, messages, keys, match.end)
                    return
            self.store.save_sync("tracking", anchors=keys)
            self.state, self.anchors = "tracking", keys
            return
        if keys == self.anchors:
            self.report("tracking")
            return
        match = match_tail(self.anchors, keys)
        if match.state == "older":
            self.report("waiting_for_tail")
            return
        if match.state != "matched":
            previous = Snapshot(
                self.store.chat,
                tuple(Message(chat=self.store.chat, metadata={"_anchor": key}) for key in self.anchors),
                True,
            )
            recovered = await self.source.recover(previous, snapshot)
            if recovered and recovered.ready and recovered.at_tail and recovered.chat == snapshot.chat:
                recovered_keys = tuple(map(message_key, recovered.messages))
                recovered_match = match_tail(self.anchors, recovered_keys)
                if recovered_match.state == "matched" and recovered_keys[-len(keys):] == keys:
                    tail_start = len(recovered_keys) - len(keys)
                    tail_index = max(0, recovered_match.end - tail_start)
                    tail = await self.source.capture_messages(snapshot, range(tail_index, len(keys)))
                    older = recovered.messages[recovered_match.end:tail_start]
                    self._save_observations(
                        keys, older + tail, recovered_keys, recovered_match.end,
                    )
                    return
            self.store.save_sync("gap", anchors=keys, gap=match.state)
            self.state, self.anchors = "gap", keys
            return
        messages = await self.source.capture_messages(snapshot, range(match.end, len(keys)))
        self._save_observations(keys, messages, keys, match.end)

    async def run(self) -> None:
        while True:
            try:
                async with self.operation_lock:
                    if not self.paused:
                        await self.step()
                await self.source.wait_for_change(self.poll_interval)
            except Exception:
                logger.exception("Message synchronization failed")
                self.report("source_error")
                await asyncio.sleep(self.poll_interval)
