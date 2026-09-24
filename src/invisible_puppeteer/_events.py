"""An event emitter with pyppeteer's surface (`on`, `once`, `emit`...).

pyppeteer uses pyee's AsyncIOEventEmitter: a coroutine handler is scheduled
as a task, a plain one is called. This does the same without the dependency.

⛔ THE ENGINE'S EVENTS ARRIVE ON THE PIPE'S READER THREAD, and asyncio objects
must not be touched from there. `emit_threadsafe` hands the emission to the
loop the session was launched on; `emit` is only called on that loop.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional


class EventEmitter:
    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._events: Dict[str, List[Callable]] = {}
        self._once: set = set()
        self._loop = loop

    def on(self, event: str, f: Optional[Callable] = None):
        """Register a handler; usable as `emitter.on(name, f)` or as a decorator."""
        def register(fn: Callable) -> Callable:
            self._events.setdefault(event, []).append(fn)
            return fn
        return register(f) if f is not None else register

    def once(self, event: str, f: Optional[Callable] = None):
        def register(fn: Callable) -> Callable:
            self._events.setdefault(event, []).append(fn)
            self._once.add((event, id(fn)))
            return fn
        return register(f) if f is not None else register

    def remove_listener(self, event: str, f: Callable) -> None:
        handlers = self._events.get(event) or []
        if f in handlers:
            handlers.remove(f)

    def remove_all_listeners(self, event: Optional[str] = None) -> None:
        if event is None:
            self._events.clear()
        else:
            self._events.pop(event, None)

    def listeners(self, event: str) -> List[Callable]:
        return list(self._events.get(event) or [])

    def listener_count(self, event: str) -> int:
        return len(self._events.get(event) or [])

    def emit(self, event: str, *args: Any) -> bool:
        handlers = list(self._events.get(event) or [])
        for fn in handlers:
            if (event, id(fn)) in self._once:
                self._once.discard((event, id(fn)))
                self.remove_listener(event, fn)
            result = fn(*args)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        return bool(handlers)

    def emit_threadsafe(self, event: str, *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self.emit, event, *args)


__all__ = ["EventEmitter"]
