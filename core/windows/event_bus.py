"""
Windows Native Event Bus (WindowsEventBus).
Captures operating system events using native Win32 Event Hooks (SetWinEventHook)
without heavy continuous polling. Employs a publisher-subscriber pattern for
instantaneous, zero-compute idle event dispatching.
"""

from __future__ import annotations

import sys
import time
import fnmatch
import logging
import threading
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

LOG = logging.getLogger("wise.event_bus")

# Win32 Constants
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
WINEVENT_OUTOFCONTEXT = 0x0000


@dataclass
class WindowsEvent:
    topic: str
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    source: str = "windows_native"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "data": self.data,
            "timestamp": self.timestamp,
            "source": self.source,
        }


EventHandler = Callable[[WindowsEvent], None]


class WindowsEventBus:
    """Central event bus capturing native OS events and distributing them to subscribers."""

    def __init__(self):
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._lock = threading.RLock()
        self._running = False
        self._hook_thread: Optional[threading.Thread] = None
        self._hook_id = None
        self._last_foreground_hwnd = 0
        self._ctypes_available = sys.platform == "win32"

    def subscribe(self, topic_pattern: str, handler: EventHandler) -> None:
        """Subscribes a handler to a topic pattern (supports wildcards like 'window.*')."""
        with self._lock:
            if topic_pattern not in self._subscribers:
                self._subscribers[topic_pattern] = []
            if handler not in self._subscribers[topic_pattern]:
                self._subscribers[topic_pattern].append(handler)
                LOG.debug("Subscribed handler to pattern '%s'", topic_pattern)

    def unsubscribe(self, topic_pattern: str, handler: EventHandler) -> None:
        with self._lock:
            if topic_pattern in self._subscribers and handler in self._subscribers[topic_pattern]:
                self._subscribers[topic_pattern].remove(handler)

    def publish(self, event: WindowsEvent) -> None:
        """Publishes an event to matching subscribers."""
        matched_handlers: List[EventHandler] = []
        with self._lock:
            for pattern, handlers in self._subscribers.items():
                if fnmatch.fnmatch(event.topic, pattern):
                    matched_handlers.extend(handlers)

        for handler in matched_handlers:
            try:
                handler(event)
            except Exception as e:
                LOG.error("Error executing handler for event '%s': %s", event.topic, e)

    def start(self) -> bool:
        """Starts the native Windows event hook thread."""
        with self._lock:
            if self._running:
                return True
            self._running = True
            self._hook_thread = threading.Thread(
                target=self._run_hook_loop,
                name="WiseWindowsEventHookThread",
                daemon=True,
            )
            self._hook_thread.start()
            LOG.info("WindowsEventBus started.")
            return True

    def stop(self) -> None:
        """Stops the hook thread cleanly."""
        with self._lock:
            self._running = False
            LOG.info("WindowsEventBus stopping...")

    def _get_window_info(self, hwnd: int) -> Dict[str, Any]:
        """Extracts window title and process ID using Win32 ctypes."""
        if not self._ctypes_available or hwnd == 0:
            return {"hwnd": hwnd, "title": "", "pid": 0, "process_name": ""}

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Title
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value

        # PID
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_pid = pid.value

        # Process name via psutil
        process_name = ""
        if process_pid > 0:
            try:
                import psutil
                proc = psutil.Process(process_pid)
                process_name = proc.name()
            except Exception:
                process_name = "unknown"

        return {
            "hwnd": hwnd,
            "title": title,
            "pid": process_pid,
            "process_name": process_name,
        }

    def _run_hook_loop(self) -> None:
        """Native Win32 event hook callback and message pump."""
        if not self._ctypes_available:
            LOG.warning("Non-Windows platform detected; Windows native hook disabled.")
            return

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        ole32 = ctypes.windll.ole32

        # Initialize COM for the thread
        ole32.CoInitialize(None)

        # WinEventProc callback prototype
        WINEVENTPROC = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,   # hWinEventHook
            wintypes.DWORD,    # event
            wintypes.HWND,     # hwnd
            wintypes.LONG,     # idObject
            wintypes.LONG,     # idChild
            wintypes.DWORD,    # idEventThread
            wintypes.DWORD,    # dwmsEventTime
        )

        def win_event_callback(hWinEventHook, event, hwnd, idObject, idChild, idEventThread, dwmsEventTime):
            if not self._running:
                return

            # Only handle standard window events (idObject == OBJID_WINDOW: 0)
            if idObject != 0 or hwnd == 0:
                return

            if event == EVENT_SYSTEM_FOREGROUND:
                if hwnd != self._last_foreground_hwnd:
                    self._last_foreground_hwnd = hwnd
                    info = self._get_window_info(hwnd)
                    if info["title"] or info["process_name"]:
                        ev = WindowsEvent(
                            topic="window.foreground_changed",
                            data=info,
                            timestamp=time.time(),
                            source="SetWinEventHook",
                        )
                        self.publish(ev)

            elif event == EVENT_OBJECT_CREATE:
                info = self._get_window_info(hwnd)
                if info["title"]:
                    ev = WindowsEvent(
                        topic="window.created",
                        data=info,
                        timestamp=time.time(),
                        source="SetWinEventHook",
                    )
                    self.publish(ev)

            elif event == EVENT_OBJECT_DESTROY:
                ev = WindowsEvent(
                    topic="window.destroyed",
                    data={"hwnd": hwnd},
                    timestamp=time.time(),
                    source="SetWinEventHook",
                )
                self.publish(ev)

        callback_ptr = WINEVENTPROC(win_event_callback)

        # Hook foreground and object lifecycle
        hook_fg = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            0,
            callback_ptr,
            0,
            0,
            WINEVENT_OUTOFCONTEXT,
        )

        hook_obj = user32.SetWinEventHook(
            EVENT_OBJECT_CREATE,
            EVENT_OBJECT_DESTROY,
            0,
            callback_ptr,
            0,
            0,
            WINEVENT_OUTOFCONTEXT,
        )

        if not hook_fg:
            LOG.warning("Failed to install SetWinEventHook; falling back to adaptive polling.")
            self._run_adaptive_polling()
            ole32.CoUninitialize()
            return

        LOG.info("Native SetWinEventHook installed successfully (Hooks: %s, %s).", hook_fg, hook_obj)

        msg = wintypes.MSG()
        try:
            # PeekMessage/DispatchMessage pump without high CPU
            while self._running:
                has_msg = user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1)  # PM_REMOVE = 1
                if has_msg:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    # Sleep when no Win32 messages are waiting: ensures ZERO CPU!
                    time.sleep(0.05)
        finally:
            if hook_fg:
                user32.UnhookWinEvent(hook_fg)
            if hook_obj:
                user32.UnhookWinEvent(hook_obj)
            ole32.CoUninitialize()
            LOG.info("SetWinEventHook unhooked cleanly.")

    def _run_adaptive_polling(self) -> None:
        """Low-frequency fallback if SetWinEventHook is unsupported."""
        LOG.info("Running adaptive low-frequency polling fallback.")
        import ctypes
        user32 = ctypes.windll.user32

        while self._running:
            try:
                hwnd = user32.GetForegroundWindow()
                if hwnd and hwnd != self._last_foreground_hwnd:
                    self._last_foreground_hwnd = hwnd
                    info = self._get_window_info(hwnd)
                    self.publish(
                        WindowsEvent(
                            topic="window.foreground_changed",
                            data=info,
                            timestamp=time.time(),
                            source="adaptive_polling",
                        )
                    )
            except Exception as e:
                LOG.debug("Adaptive polling error: %s", e)
            time.sleep(1.0)  # Low frequency fallback


# Singleton instance
_GLOBAL_EVENT_BUS: Optional[WindowsEventBus] = None
_EB_LOCK = threading.Lock()


def get_event_bus() -> WindowsEventBus:
    global _GLOBAL_EVENT_BUS
    if _GLOBAL_EVENT_BUS is None:
        with _EB_LOCK:
            if _GLOBAL_EVENT_BUS is None:
                _GLOBAL_EVENT_BUS = WindowsEventBus()
    return _GLOBAL_EVENT_BUS
