"""
Unified Computer Control Abstraction (ComputerControl).
Consolidates all discrete system, GUI, and filesystem capabilities into a single,
hierarchical interface for the WISE agent:
Computer.get_state(), Computer.observe(), Computer.open_app(), Computer.focus_window(),
Computer.click(), Computer.type_text(), Computer.send_hotkey(), Computer.run_command(),
Computer.read_file(), Computer.write_file(), Computer.move_file(), Computer.close_app(),
and Computer.verify().
Enforces hierarchical execution and routes all operations through the SecurityGate.
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import logging
import threading
import subprocess
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from core.context.world_model import get_world_model_manager, ComputerWorldModel
from core.security.security_gate import get_security_gate, SecurityContext, SecurityEvaluation, ActionTier

LOG = logging.getLogger("wise.computer_control")


class ComputerControl:
    """The unified physical and operational control interface of WISE on Windows."""

    def __init__(self, security_context: Optional[SecurityContext] = None):
        self.world_model_mgr = get_world_model_manager()
        self.security_gate = get_security_gate()
        self.security_context = security_context or SecurityContext()
        self._ctypes_available = sys.platform == "win32"

    def get_state(self, scope: str = "summary") -> Dict[str, Any]:
        """Returns the current state of the computer from the ComputerWorldModel."""
        ev = self.security_gate.evaluate("get_state", {"scope": scope}, self.security_context)
        if not ev.allowed:
            return {"error": ev.reason}

        self.world_model_mgr.refresh_active_window()
        if scope == "full":
            return self.world_model_mgr.get_snapshot()
        return {
            "summary": self.world_model_mgr.get_prompt_context(),
            "active_window": self.world_model_mgr.model.active_window.__dict__,
            "open_windows_count": len(self.world_model_mgr.model.open_windows),
        }

    def observe(self, scope: str = "auto", hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Hierarchical computer observation: Level 1 (UI Tree) -> Level 2 (Capture) -> Level 3 (OCR)."""
        ev = self.security_gate.evaluate("observe", {"scope": scope}, self.security_context)
        if not ev.allowed:
            return {"error": ev.reason}

        from core.vision import get_vision_manager
        vm = get_vision_manager()
        target_hwnd = hwnd or self.world_model_mgr.model.active_window.hwnd
        obs = vm.observe(hwnd=target_hwnd, scope=scope)
        return obs.to_dict()

    def read_screen(self, x: int = 0, y: int = 0, width: int = 0, height: int = 0) -> Dict[str, Any]:
        """Captures screen or specific region on demand without continuous background capture."""
        ev = self.security_gate.evaluate("read_screen", {"x": x, "y": y, "width": width, "height": height}, self.security_context)
        if not ev.allowed:
            return {"error": ev.reason}

        from core.vision import get_screen_capture_engine
        engine = get_screen_capture_engine()
        if width > 0 and height > 0:
            res = engine.capture_region(x, y, width, height)
        else:
            res = engine.capture_full_screen()
        return res.to_dict()

    def open_app(self, app_name_or_path: str) -> Dict[str, Any]:
        """Hierarchical app launcher: ShellExecuteW / os.startfile / subprocess."""
        ev = self.security_gate.evaluate("open_app", {"app": app_name_or_path}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason, "requires_confirmation": ev.requires_confirmation}

        try:
            if Path(app_name_or_path).is_file():
                argv = [app_name_or_path]
            else:
                argv = [part.strip('"') for part in shlex.split(app_name_or_path, posix=False)]
            if not argv:
                return {"success": False, "error": "Empty application target"}
            argv[0] = shutil.which(argv[0]) or argv[0]
            if Path(argv[0]).stem.lower() in {
                "cmd", "powershell", "pwsh", "python", "python3", "pythonw",
                "wscript", "cscript", "mshta", "bash", "sh", "node",
            }:
                code_ev = self.security_gate.evaluate("run_shell", {"command": app_name_or_path}, self.security_context)
                if not code_ev.allowed:
                    return {"success": False, "error": code_ev.reason, "requires_confirmation": code_ev.requires_confirmation}
            if sys.platform == "win32":
                # Level 1: Win32 CreateProcessW explicitly targeting WinSta0\Default
                try:
                    import ctypes
                    from ctypes import wintypes
                    k = ctypes.windll.kernel32

                    class STARTUPINFO(ctypes.Structure):
                        _fields_ = [
                            ("cb", wintypes.DWORD),
                            ("lpReserved", wintypes.LPWSTR),
                            ("lpDesktop", wintypes.LPWSTR),
                            ("lpTitle", wintypes.LPWSTR),
                            ("dwX", wintypes.DWORD),
                            ("dwY", wintypes.DWORD),
                            ("dwXSize", wintypes.DWORD),
                            ("dwYSize", wintypes.DWORD),
                            ("dwXCountChars", wintypes.DWORD),
                            ("dwYCountChars", wintypes.DWORD),
                            ("dwFillAttribute", wintypes.DWORD),
                            ("dwFlags", wintypes.DWORD),
                            ("wShowWindow", wintypes.WORD),
                            ("cbReserved2", wintypes.WORD),
                            ("lpReserved2", ctypes.c_char_p),
                            ("hStdInput", wintypes.HANDLE),
                            ("hStdOutput", wintypes.HANDLE),
                            ("hStdError", wintypes.HANDLE),
                        ]

                    class PROCESS_INFORMATION(ctypes.Structure):
                        _fields_ = [
                            ("hProcess", wintypes.HANDLE),
                            ("hThread", wintypes.HANDLE),
                            ("dwProcessId", wintypes.DWORD),
                            ("dwThreadId", wintypes.DWORD),
                        ]

                    si = STARTUPINFO()
                    si.cb = ctypes.sizeof(STARTUPINFO)
                    si.lpDesktop = "WinSta0\\Default"
                    si.dwFlags = 1
                    si.wShowWindow = 1
                    pi = PROCESS_INFORMATION()

                    resolved = argv[0]
                    if not Path(resolved).is_file() or Path(resolved).suffix.lower() != ".exe":
                        raise ValueError("Application must resolve to an executable file")
                    cmd_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))

                    if k.CreateProcessW(None, cmd_line, None, None, False, 0, None, None, ctypes.byref(si), ctypes.byref(pi)):
                        k.CloseHandle(pi.hProcess)
                        k.CloseHandle(pi.hThread)
                        time.sleep(0.5)
                        self.world_model_mgr.refresh_active_window()
                        return {"success": True, "launched": app_name_or_path, "pid": pi.dwProcessId, "method": "CreateProcessW_Desktop"}
                except Exception as ex:
                    LOG.debug("CreateProcessW on Default desktop failed: %s", ex)

                # Level 2: Win32 os.startfile fallback
                try:
                    os.startfile(argv[0], arguments=subprocess.list2cmdline(argv[1:]))
                    time.sleep(0.5)
                    self.world_model_mgr.refresh_active_window()
                    return {"success": True, "launched": app_name_or_path, "method": "os.startfile"}
                except Exception:
                    pass

                # Level 3: Subprocess execution fallback
                proc = subprocess.Popen(
                    argv,
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.5)
                self.world_model_mgr.refresh_active_window()
                return {"success": True, "launched": app_name_or_path, "pid": proc.pid, "method": "subprocess"}

            return {"success": False, "error": "Non-Windows platform"}
        except Exception as e:
            LOG.error("Failed to open app '%s': %s", app_name_or_path, e)
            return {"success": False, "error": str(e)}

    def focus_window(self, window_identifier: Union[str, int]) -> Dict[str, Any]:
        """Brings target window to the foreground via HWND or title matching."""
        ev = self.security_gate.evaluate("focus_window", {"target": str(window_identifier)}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}

        if not self._ctypes_available:
            return {"success": False, "error": "Win32 unavailable"}

        import ctypes
        user32 = ctypes.windll.user32

        target_hwnd = 0
        if isinstance(window_identifier, int):
            target_hwnd = window_identifier
        else:
            # Search among open windows in world model
            self.world_model_mgr.refresh_open_windows()
            query = str(window_identifier).lower()
            for win in self.world_model_mgr.model.open_windows:
                if query in win.get("title", "").lower() or query in win.get("process_name", "").lower():
                    target_hwnd = win.get("hwnd", 0)
                    break

        if not target_hwnd:
            return {"success": False, "error": f"Window '{window_identifier}' not found"}

        # Win32 bring to front
        user32.ShowWindow(target_hwnd, 9)  # SW_RESTORE = 9
        res = user32.SetForegroundWindow(target_hwnd)
        time.sleep(0.1)
        self.world_model_mgr.refresh_active_window()
        return {"success": bool(res), "hwnd": target_hwnd}

    def close_app(self, identifier: Union[str, int], graceful: bool = True) -> Dict[str, Any]:
        """Closes target app gracefully via WM_CLOSE, or terminates PID."""
        ev = self.security_gate.evaluate("close_app", {"target": str(identifier), "graceful": graceful}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason, "requires_confirmation": ev.requires_confirmation}

        if not self._ctypes_available:
            return {"success": False, "error": "Win32 unavailable"}

        import ctypes
        user32 = ctypes.windll.user32

        target_hwnd = 0
        target_pid = 0
        if isinstance(identifier, int):
            target_hwnd = identifier
        else:
            query = str(identifier).lower()
            self.world_model_mgr.refresh_open_windows()
            for win in self.world_model_mgr.model.open_windows:
                if query in win.get("title", "").lower() or query in win.get("process_name", "").lower():
                    target_hwnd = win.get("hwnd", 0)
                    target_pid = win.get("pid", 0)
                    break

        if target_hwnd and graceful:
            WM_CLOSE = 0x0010
            user32.PostMessageW(target_hwnd, WM_CLOSE, 0, 0)
            time.sleep(0.3)
            return {"success": True, "method": "WM_CLOSE", "hwnd": target_hwnd}

        if target_pid:
            import psutil
            try:
                p = psutil.Process(target_pid)
                p.terminate()
                return {"success": True, "method": "terminate", "pid": target_pid}
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": f"Target '{identifier}' not found"}

    def run_command(self, command: str, timeout: int = 30) -> Dict[str, Any]:
        """Runs a native Windows command via PowerShell with timeout and safety gate."""
        ev = self.security_gate.evaluate("run_command", {"command": command}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason, "requires_confirmation": ev.requires_confirmation}

        try:
            # Execute via powershell.exe
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return {
                "success": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Command execution timed out"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def read_file(self, path: str, encoding: str = "utf-8", max_bytes: int = 500000) -> Dict[str, Any]:
        """Reads file content from anywhere in the allowed filesystem."""
        ev = self.security_gate.evaluate("read_file", {"path": path}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}

        try:
            target = Path(path).resolve()
            if not target.exists() or not target.is_file():
                return {"success": False, "error": f"File not found: {path}"}

            content = target.read_text(encoding=encoding, errors="replace")[:max_bytes]
            return {"success": True, "path": str(target), "content": content, "size": len(content)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def write_file(self, path: str, content: str, encoding: str = "utf-8") -> Dict[str, Any]:
        """Writes file content, enforcing system folder protection via SecurityGate."""
        ev = self.security_gate.evaluate("write_file", {"path": path}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason, "requires_confirmation": ev.requires_confirmation}

        try:
            target = Path(path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding, errors="replace")
            return {"success": True, "path": str(target), "bytes_written": len(content.encode(encoding))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def move_file(self, src: str, dst: str) -> Dict[str, Any]:
        """Moves or renames a file or directory."""
        ev = self.security_gate.evaluate("move_file", {"path": src, "destination": dst}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason, "requires_confirmation": ev.requires_confirmation}

        try:
            src_path = Path(src).resolve()
            dst_path = Path(dst).resolve()
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            return {"success": True, "src": str(src_path), "dst": str(dst_path)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> Dict[str, Any]:
        """Simulates physical mouse click via Win32 mouse_event."""
        ev = self.security_gate.evaluate("click", {"x": x, "y": y, "button": button}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}

        if not self._ctypes_available:
            return {"success": False, "error": "Win32 unavailable"}

        import ctypes
        user32 = ctypes.windll.user32

        # Set cursor position
        user32.SetCursorPos(x, y)
        time.sleep(0.02)

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010

        if button == "right":
            down = MOUSEEVENTF_RIGHTDOWN
            up = MOUSEEVENTF_RIGHTUP
        else:
            down = MOUSEEVENTF_LEFTDOWN
            up = MOUSEEVENTF_LEFTUP

        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.02)
        user32.mouse_event(up, 0, 0, 0, 0)

        if double:
            time.sleep(0.08)
            user32.mouse_event(down, 0, 0, 0, 0)
            time.sleep(0.02)
            user32.mouse_event(up, 0, 0, 0, 0)

        return {"success": True, "x": x, "y": y, "button": button, "double": double}

    def type_text(self, text: str) -> Dict[str, Any]:
        """Types text into the active foreground window using Win32 SendInput or keybd_event."""
        ev = self.security_gate.evaluate("type_text", {"length": len(text)}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}

        if not self._ctypes_available:
            return {"success": False, "error": "Win32 unavailable"}

        import ctypes
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004

        for char in text:
            code = ord(char)
            user32.keybd_event(0, code, KEYEVENTF_UNICODE, 0)
            user32.keybd_event(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(0.005)

        return {"success": True, "characters_typed": len(text)}

    def send_hotkey(self, keys: List[str]) -> Dict[str, Any]:
        """Simulates key combo hotkeys (e.g. ['ctrl', 'c'], ['win', 'r'])."""
        ev = self.security_gate.evaluate("send_hotkey", {"keys": keys}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}

        if not self._ctypes_available:
            return {"success": False, "error": "Win32 unavailable"}

        import ctypes
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002

        VK_MAP = {
            "ctrl": 0x11, "control": 0x11,
            "alt": 0x12,
            "shift": 0x10,
            "win": 0x5B, "windows": 0x5B,
            "enter": 0x0D, "return": 0x0D,
            "esc": 0x1B, "escape": 0x1B,
            "tab": 0x09,
            "space": 0x20,
            "backspace": 0x08,
            "c": 0x43, "v": 0x56, "r": 0x52, "s": 0x53, "a": 0x41, "z": 0x5A,
        }

        vk_codes = []
        for k in keys:
            normalized = k.lower()
            code = VK_MAP.get(normalized)
            if code is None and len(normalized) == 1:
                code = ord(normalized.upper())
            if code:
                vk_codes.append(code)

        # Press down in order
        for code in vk_codes:
            user32.keybd_event(code, 0, 0, 0)
        time.sleep(0.02)
        # Release in reverse order
        for code in reversed(vk_codes):
            user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)

        return {"success": True, "keys": keys}

    def verify(self, condition: Dict[str, Any]) -> Dict[str, Any]:
        """Closed-loop verification: confirms if an expected state holds true."""
        kind = condition.get("type")
        
        if kind == "file_exists":
            target = Path(condition["path"])
            exists = target.exists()
            return {"verified": exists, "path": str(target), "details": "File exists on disk" if exists else "File missing"}

        elif kind == "window_active":
            self.world_model_mgr.refresh_active_window()
            active = self.world_model_mgr.model.active_window
            expected_title = condition.get("title", "").lower()
            match = expected_title in active.title.lower()
            return {"verified": match, "actual_title": active.title, "expected": expected_title}

        elif kind == "process_running":
            expected_name = condition.get("name", "").lower()
            import psutil
            running = any(expected_name in (p.info.get("name") or "").lower() for p in psutil.process_iter(["name"]))
            return {"verified": running, "process": expected_name}

        elif kind == "text_visible":
            from core.vision import get_vision_manager, get_ui_tree_extractor
            vm = get_vision_manager()
            target_text = condition.get("text", "")
            obs = vm.observe(scope="auto")
            visible = False
            if obs.ui_tree:
                matches = get_ui_tree_extractor().find_elements_by_text(obs.ui_tree, target_text)
                visible = len(matches) > 0
            if not visible and obs.ocr and target_text.lower() in obs.ocr.full_text.lower():
                visible = True
            return {"verified": visible, "text": target_text, "level_used": obs.level_used.value}

        return {"verified": False, "error": f"Unknown verification type: {kind}"}

    # ==========================================================================
    # P0.3 WISE Hands / Hierarchical Computer Use Integration
    # ==========================================================================

    def mouse_click(self, x: int, y: int, button: str = "left", double: bool = False) -> Dict[str, Any]:
        """Dispatches mouse click via SendInput under SecurityGate supervision."""
        ev = self.security_gate.evaluate("mouse_click", {"x": x, "y": y, "button": button, "double": double}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.input_driver import get_input_driver
        driver = get_input_driver()
        ok = driver.double_click(x, y) if double else driver.click(button, x, y)
        return {"success": ok, "x": x, "y": y, "button": button, "double": double}

    def mouse_move(self, x: int, y: int) -> Dict[str, Any]:
        """Moves cursor to exact coordinates under SecurityGate supervision."""
        ev = self.security_gate.evaluate("mouse_move", {"x": x, "y": y}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.input_driver import get_input_driver
        ok = get_input_driver().move_to(x, y)
        return {"success": ok, "x": x, "y": y}

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.2) -> Dict[str, Any]:
        """Performs smooth drag and drop from (x1, y1) to (x2, y2)."""
        ev = self.security_gate.evaluate("mouse_drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.input_driver import get_input_driver
        ok = get_input_driver().drag_and_drop(x1, y1, x2, y2, duration=duration)
        return {"success": ok, "from": (x1, y1), "to": (x2, y2)}

    def mouse_scroll(self, delta: int, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Scrolls mouse wheel."""
        ev = self.security_gate.evaluate("mouse_scroll", {"delta": delta}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.input_driver import get_input_driver
        ok = get_input_driver().scroll(delta, x, y)
        return {"success": ok, "delta": delta}

    def keyboard_type(self, text: str, delay: float = 0.01) -> Dict[str, Any]:
        """Types Unicode text via SendInput."""
        ev = self.security_gate.evaluate("keyboard_type", {"text": text}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.input_driver import get_input_driver
        count = get_input_driver().type_text(text, delay_per_char=delay)
        return {"success": count == len(text), "dispatched_chars": count}

    def keyboard_hotkey(self, *keys: str) -> Dict[str, Any]:
        """Executes a multi-key shortcut sequence."""
        ev = self.security_gate.evaluate("keyboard_hotkey", {"keys": list(keys)}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.input_driver import get_input_driver
        ok = get_input_driver().hotkey(*keys)
        return {"success": ok, "keys": list(keys)}

    def focus_window(self, query_or_hwnd: Union[str, int]) -> Dict[str, Any]:
        """Brings the target window to the foreground."""
        ev = self.security_gate.evaluate("focus_window", {"target": query_or_hwnd}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.window_manager import get_window_manager
        wm = get_window_manager()
        hwnd = query_or_hwnd if isinstance(query_or_hwnd, int) else None
        if not hwnd and isinstance(query_or_hwnd, str):
            w = wm.find_window(query_or_hwnd)
            if w:
                hwnd = w.hwnd
        if not hwnd:
            return {"success": False, "error": f"Window not found for query '{query_or_hwnd}'"}
        ok = wm.bring_to_front(hwnd)
        return {"success": ok, "hwnd": hwnd}

    def close_window(self, query_or_hwnd: Union[str, int], force: bool = False) -> Dict[str, Any]:
        """Closes target window under SecurityGate evaluation."""
        ev = self.security_gate.evaluate("close_window", {"target": query_or_hwnd, "force": force}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.windows.window_manager import get_window_manager
        wm = get_window_manager()
        hwnd = query_or_hwnd if isinstance(query_or_hwnd, int) else None
        if not hwnd and isinstance(query_or_hwnd, str):
            w = wm.find_window(query_or_hwnd)
            if w:
                hwnd = w.hwnd
        if not hwnd:
            return {"success": False, "error": f"Window not found for query '{query_or_hwnd}'"}
        ok = wm.close_window(hwnd, force=force)
        return {"success": ok, "hwnd": hwnd}

    def click_element(self, text_query: str, hwnd: Optional[int] = None) -> Dict[str, Any]:
        """Hierarchical element clicking (UIA first -> OCR -> SendInput click)."""
        ev = self.security_gate.evaluate("click_element", {"text_query": text_query, "hwnd": hwnd}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.hands import get_wise_hands, ComputerActionType
        hands = get_wise_hands()
        rec = hands.execute_closed_loop_action(
            ComputerActionType.CLICK,
            {"text_query": text_query, "hwnd": hwnd},
            security_context=self.security_context,
        )
        return rec.to_dict()

    def execute_closed_loop(
        self,
        action_type: str,
        params: Dict[str, Any],
        verification_condition: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Executes full Observe -> Understand -> Act -> Observe -> Verify -> Recover cycle."""
        ev = self.security_gate.evaluate("execute_closed_loop", {"action_type": action_type, "params": params}, self.security_context)
        if not ev.allowed:
            return {"success": False, "error": ev.reason}
        from core.hands import get_wise_hands, ComputerActionType
        hands = get_wise_hands()
        try:
            act_enum = ComputerActionType(action_type)
        except ValueError:
            return {"success": False, "error": f"Invalid action_type: {action_type}"}
        rec = hands.execute_closed_loop_action(
            act_enum,
            params,
            verification_condition=verification_condition,
            security_context=self.security_context,
        )
        return rec.to_dict()

    def get_world_state(self, force_fresh: bool = False) -> Dict[str, Any]:
        """Queries the complete unified WISEWorldState snapshot."""
        from core.context.world_state import get_world_state_engine
        engine = get_world_state_engine()
        state = engine.get_current_world_state(force_fresh=force_fresh)
        return state.to_dict()

    def orchestrate(
        self,
        intent: str,
        steps: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Orchestrates an end-to-end cognitive cycle under SecurityGate supervision."""
        from core.orchestrator import get_closed_loop_orchestrator, OrchestrationStep
        from core.hands import ComputerActionType

        orc_steps: List[OrchestrationStep] = []
        for s in steps:
            act_type = ComputerActionType(s["action_type"])
            orc_steps.append(OrchestrationStep(
                action_type=act_type,
                params=s.get("params", {}),
                verification_spec=s.get("verification_spec"),
                max_retries=s.get("max_retries", 2),
            ))

        orc = get_closed_loop_orchestrator(security_context=self.security_context)
        res = orc.run_cycle(intent=intent, steps=orc_steps)
        return res.to_dict()

    def orchestrate_intent(
        self,
        intent: str,
        max_replans: int = 3,
    ) -> Dict[str, Any]:
        """Autonomously decomposes a natural language intent into an optimized plan,
        validates each action against SecurityGate, executes with closed-loop verification,
        and dynamically re-plans if obstacles or failures occur.
        """
        from core.orchestrator import get_closed_loop_orchestrator
        orc = get_closed_loop_orchestrator(security_context=self.security_context)
        res = orc.orchestrate_intent(intent=intent, max_replans=max_replans)
        return res.to_dict()

    def speak(self, text: str, async_playback: bool = True) -> Dict[str, Any]:
        """Synthesizes text output using the native voice subsystem."""
        from core.voice import get_voice_runtime
        vr = get_voice_runtime()
        tts_res = vr.tts.speak(text, async_playback=async_playback)
        return tts_res.to_dict()

    def voice_interact(
        self,
        transcript_or_audio: Union[str, bytes],
    ) -> Dict[str, Any]:
        """Routes voice input through VAD, STT, Cognitive Brain, and TTS."""
        from core.voice import get_voice_runtime
        vr = get_voice_runtime()
        res = vr.interact(transcript_or_audio)
        return res.to_dict()


# Global singleton
_GLOBAL_COMPUTER_CONTROL: Optional[ComputerControl] = None
_CC_LOCK = threading.Lock()


def get_computer_control(security_context: Optional[SecurityContext] = None) -> ComputerControl:
    global _GLOBAL_COMPUTER_CONTROL
    if _GLOBAL_COMPUTER_CONTROL is None:
        with _CC_LOCK:
            if _GLOBAL_COMPUTER_CONTROL is None:
                _GLOBAL_COMPUTER_CONTROL = ComputerControl(security_context=security_context)
    return _GLOBAL_COMPUTER_CONTROL
