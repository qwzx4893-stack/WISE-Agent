#!/usr/bin/env python3
"""
WISE Canvas Test Helper
Renders a custom GUI window with raw GDI/Canvas drawing that is invisible
to Windows UI Automation (0 UIA child elements), used to test Level 3 OCR escalation.
"""

import sys
import ctypes

def main():
    try:
        u = ctypes.windll.user32
        h_def = u.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_def:
            u.SetThreadDesktop(h_def)
    except Exception as e:
        pass

    import tkinter as tk
    root = tk.Tk()
    root.title("WISE_CANVAS_FIELD_TEST")
    root.geometry("450x320+250+250")
    root.configure(bg="#1E1E2E")

    canvas = tk.Canvas(root, width=450, height=320, bg="#1E1E2E", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    # Pure pixel drawn button (NOT exposed to UIA controls)
    btn_rect = canvas.create_rectangle(80, 90, 370, 170, fill="#4A90E2", outline="#FFFFFF", width=2)
    btn_text = canvas.create_text(225, 130, text="PROCEED_ACTION", font=("Arial", 16, "bold"), fill="#FFFFFF")

    clicks = 0
    def on_click(event):
        nonlocal clicks
        if 80 <= event.x <= 370 and 90 <= event.y <= 170:
            clicks += 1
            canvas.itemconfig(btn_rect, fill="#27AE60")
            canvas.itemconfig(btn_text, text="ACTION_TRIGGERED")
            print(f"CANVAS_CLICKED_AT_{event.x}_{event.y}_COUNT_{clicks}", flush=True)

    canvas.bind("<Button-1>", on_click)

    # Signal ready to test runner
    print("CANVAS_WINDOW_READY", flush=True)

    # Auto-close after 30 seconds if not closed by test
    root.after(30000, root.destroy)
    root.mainloop()

if __name__ == "__main__":
    main()
