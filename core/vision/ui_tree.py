"""
Windows UI Automation & Accessibility Tree Extractor (UITreeExtractor).
Extracts hierarchical structural UI information (buttons, inputs, menus, text, rectangles)
from the active window or root desktop without capturing pixels or invoking heavy vision models.
Level 1 of the WISE Hierarchical Perception Engine.
Enhanced in P0.3 with deep Windows UI Automation (UIA) COM support for modern apps (WinUI, Electron, Chromium).
"""

from __future__ import annotations

import os
import sys
import time
import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

LOG = logging.getLogger("wise.ui_tree")


@dataclass
class BoundingBox:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> Tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def to_dict(self) -> Dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class UIElementNode:
    hwnd: int
    name: str
    control_type: str
    class_name: str
    bounding_box: BoundingBox
    is_enabled: bool = True
    is_visible: bool = True
    value: Optional[str] = None
    children: List[UIElementNode] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "name": self.name,
            "control_type": self.control_type,
            "class_name": self.class_name,
            "bounding_box": self.bounding_box.to_dict(),
            "center": self.bounding_box.center,
            "is_enabled": self.is_enabled,
            "is_visible": self.is_visible,
            "value": self.value,
            "children": [c.to_dict() for c in self.children],
        }

    def format_text_tree(self, indent: int = 0) -> str:
        """Serializes the element into a high-signal, compact indented string for LLM prompts."""
        prefix = "  " * indent
        name_str = f"'{self.name}'" if self.name else "<unnamed>"
        val_str = f" value='{self.value}'" if self.value else ""
        box = self.bounding_box
        line = f"{prefix}- [{self.control_type}] {name_str}{val_str} (pos: {box.x},{box.y} size: {box.width}x{box.height})"

        lines = [line]
        for c in self.children:
            lines.append(c.format_text_tree(indent + 1))
        return "\n".join(lines)


class UITreeExtractor:
    """Extracts structured UI hierarchy from native Windows controls and deep UIA accessibility layers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._is_win32 = sys.platform == "win32"
        self._uia_available = False
        self._init_uia()

    def _init_uia(self) -> None:
        """Checks and initializes uiautomation library availability."""
        if not self._is_win32:
            return
        try:
            import uiautomation as auto
            self._uia_available = True
        except ImportError:
            self._uia_available = False

    def _ensure_interactive_desktop(self) -> bool:
        """Ensures calling thread is attached to the interactive Default desktop."""
        if not self._is_win32:
            return False
        try:
            import ctypes
            u = ctypes.windll.user32
            h_def = u.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_def:
                return bool(u.SetThreadDesktop(h_def))
        except Exception:
            pass
        return False

    def get_window_ui_tree(self, hwnd: int = 0, max_depth: int = 4) -> Optional[UIElementNode]:
        """
        Extracts the UI element tree for a target window handle.
        Uses Deep UIA for modern apps (Electron, WinUI, Chromium) with fallback to Win32 EnumChildWindows.
        """
        if not self._is_win32 or hwnd == 0:
            LOG.debug("UI tree unavailable: no interactive Windows window handle")
            return None

        self._ensure_interactive_desktop()

        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            if not user32.IsWindow(hwnd):
                LOG.debug("UI tree unavailable: hwnd %d is no longer a window", hwnd)
                return None

            # Attempt Deep UIA Extraction first
            if self._uia_available:
                uia_tree = self._extract_via_uia(hwnd, max_depth)
                if uia_tree and (len(uia_tree.children) > 0 or uia_tree.name):
                    return uia_tree

            # Fallback to Win32 Child Windows Traversal
            return self._extract_via_win32(hwnd, max_depth)
        except Exception as e:
            LOG.error("Failed to extract UI tree for hwnd %d: %s", hwnd, e)
            return None

    def _extract_via_uia(self, hwnd: int, max_depth: int) -> Optional[UIElementNode]:
        """Extracts hierarchy using Windows UI Automation COM interface."""
        try:
            import uiautomation as auto
            ctrl = auto.ControlFromHandle(hwnd)
            if not ctrl:
                return None

            rect = ctrl.BoundingRectangle
            w = max(0, rect.width()) if hasattr(rect, "width") else max(0, rect.right - rect.left)
            h = max(0, rect.height()) if hasattr(rect, "height") else max(0, rect.bottom - rect.top)

            root = UIElementNode(
                hwnd=hwnd,
                name=ctrl.Name or "",
                control_type=ctrl.ControlTypeName.replace("Control", "") if ctrl.ControlTypeName else "Window",
                class_name=ctrl.ClassName or "",
                bounding_box=BoundingBox(x=rect.left, y=rect.top, width=w, height=h),
                is_enabled=ctrl.IsEnabled,
                is_visible=not ctrl.IsOffscreen if hasattr(ctrl, "IsOffscreen") else True,
            )

            # Populate UIA children recursively
            if max_depth > 0:
                self._populate_uia_children(ctrl, root, max_depth - 1)

            return root
        except Exception as e:
            LOG.debug("UIA extraction failed on hwnd %d: %s", hwnd, e)
            return None

    def _populate_uia_children(self, uia_parent: Any, node_parent: UIElementNode, remaining_depth: int) -> None:
        """Traverses UIA children up to remaining depth."""
        if remaining_depth <= 0:
            return
        try:
            children = uia_parent.GetChildren()
            node_children: List[UIElementNode] = []
            for c in children[:25]:  # Bound to 25 children per branch to protect context size
                try:
                    r = c.BoundingRectangle
                    cw = max(0, r.width()) if hasattr(r, "width") else max(0, r.right - r.left)
                    ch = max(0, r.height()) if hasattr(r, "height") else max(0, r.bottom - r.top)

                    val = None
                    # Read ValuePattern if available (e.g. text in edit boxes)
                    if hasattr(c, "GetValuePattern") and c.GetValuePattern():
                        val = c.GetValuePattern().Value

                    c_type = c.ControlTypeName.replace("Control", "") if c.ControlTypeName else "Element"
                    child_node = UIElementNode(
                        hwnd=c.NativeWindowHandle or node_parent.hwnd,
                        name=c.Name or "",
                        control_type=c_type,
                        class_name=c.ClassName or "",
                        bounding_box=BoundingBox(x=r.left, y=r.top, width=cw, height=ch),
                        is_enabled=c.IsEnabled,
                        is_visible=not c.IsOffscreen if hasattr(c, "IsOffscreen") else True,
                        value=val,
                    )
                    if remaining_depth > 1:
                        self._populate_uia_children(c, child_node, remaining_depth - 1)
                    node_children.append(child_node)
                except Exception:
                    continue
            node_parent.children = node_children
        except Exception as e:
            LOG.debug("Error populating UIA children: %s", e)

    def _extract_via_win32(self, hwnd: int, max_depth: int) -> UIElementNode:
        """Native Win32 fallback extraction using GetWindowRect and EnumChildWindows."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value

        class_buff = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buff, 256)
        class_name = class_buff.value

        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        box = BoundingBox(
            x=r.left,
            y=r.top,
            width=max(0, r.right - r.left),
            height=max(0, r.bottom - r.top),
        )

        root = UIElementNode(
            hwnd=hwnd,
            name=title,
            control_type="Window",
            class_name=class_name,
            bounding_box=box,
            is_enabled=bool(user32.IsWindowEnabled(hwnd)),
            is_visible=bool(user32.IsWindowVisible(hwnd)),
        )

        if max_depth > 0:
            self._populate_child_windows(root, max_depth - 1)

        return root

    def _populate_child_windows(self, parent_node: UIElementNode, remaining_depth: int) -> None:
        """Recursively enumerates child HWNDs using EnumChildWindows."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        child_nodes: List[UIElementNode] = []

        def enum_child_proc(hwnd: int, lParam: int) -> bool:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value

                class_buff = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buff, 256)
                c_name = class_buff.value

                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                box = BoundingBox(
                    x=r.left,
                    y=r.top,
                    width=max(0, r.right - r.left),
                    height=max(0, r.bottom - r.top),
                )

                control_type = self._classify_control_type(c_name)
                child_node = UIElementNode(
                    hwnd=hwnd,
                    name=title,
                    control_type=control_type,
                    class_name=c_name,
                    bounding_box=box,
                    is_enabled=bool(user32.IsWindowEnabled(hwnd)),
                    is_visible=True,
                )
                child_nodes.append(child_node)
            return True

        cb = WNDENUMPROC(enum_child_proc)
        user32.EnumChildWindows(parent_node.hwnd, cb, 0)
        parent_node.children = child_nodes[:30]

    def _classify_control_type(self, class_name: str) -> str:
        """Classifies common Windows control class names into intuitive roles."""
        lower = class_name.lower()
        if "button" in lower:
            return "Button"
        elif "edit" in lower or "richedit" in lower:
            return "Edit"
        elif "static" in lower or "label" in lower:
            return "Label"
        elif "combobox" in lower or "combo" in lower:
            return "ComboBox"
        elif "listbox" in lower or "listview" in lower or "syslistview" in lower:
            return "ListView"
        elif "treeview" in lower or "systreeview" in lower:
            return "TreeView"
        elif "tab" in lower or "systabcontrol" in lower:
            return "TabControl"
        elif "scrollbar" in lower:
            return "ScrollBar"
        elif "menu" in lower:
            return "Menu"
        elif "toolbar" in lower:
            return "ToolBar"
        elif "status" in lower:
            return "StatusBar"
        return "CustomControl"

    def find_elements_by_text(self, root: UIElementNode, text_query: str) -> List[UIElementNode]:
        """Searches the element tree for elements matching a text query."""
        results = []
        q = text_query.lower()

        def _search(node: UIElementNode):
            if q in node.name.lower() or (node.value and q in node.value.lower()):
                results.append(node)
            for c in node.children:
                _search(c)

        _search(root)
        return results


# Singleton
_GLOBAL_UI_TREE_EXTRACTOR: Optional[UITreeExtractor] = None
_UTE_LOCK = threading.Lock()


def get_ui_tree_extractor() -> UITreeExtractor:
    global _GLOBAL_UI_TREE_EXTRACTOR
    if _GLOBAL_UI_TREE_EXTRACTOR is None:
        with _UTE_LOCK:
            if _GLOBAL_UI_TREE_EXTRACTOR is None:
                _GLOBAL_UI_TREE_EXTRACTOR = UITreeExtractor()
    return _GLOBAL_UI_TREE_EXTRACTOR
