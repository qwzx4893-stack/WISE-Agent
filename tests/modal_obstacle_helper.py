#!/usr/bin/env python3
"""
WISE Modal Obstacle Test Helper
Spawns a main application window with an unexpected modal confirmation dialog
titled 'Confirm Save As' to test real obstacle detection and self-healing.
"""

import sys
import time
import ctypes

def main():
    try:
        u = ctypes.windll.user32
        h_def = u.OpenDesktopW("Default", 0, False, 0x01FF)
        if h_def:
            u.SetThreadDesktop(h_def)
    except Exception:
        pass

    import tkinter as tk
    root = tk.Tk()
    root.title("WISE_MODAL_TEST_APP")
    root.geometry("400x250+300+300")
    root.configure(bg="#2E3440")

    lbl = tk.Label(root, text="Target Application Window", fg="#ECEFF4", bg="#2E3440", font=("Arial", 12))
    lbl.pack(pady=40)

    # Spawn modal dialog
    dialog = tk.Toplevel(root)
    dialog.title("Confirm Save As")
    dialog.geometry("320x160+340+340")
    dialog.configure(bg="#3B4252")
    dialog.transient(root)
    dialog.grab_set()

    d_lbl = tk.Label(dialog, text="File already exists. Replace?", fg="#E5E9F0", bg="#3B4252", font=("Arial", 11))
    d_lbl.pack(pady=20)

    resolved = False

    def on_confirm(event=None):
        nonlocal resolved
        resolved = True
        print("MODAL_OBSTACLE_CONFIRMED_AND_RESOLVED", flush=True)
        dialog.destroy()

    btn = tk.Button(dialog, text="Confirm", command=on_confirm, bg="#A3BE8C", fg="#2E3440", font=("Arial", 10, "bold"), width=12)
    btn.pack(pady=10)
    btn.focus_set()

    dialog.bind("<Return>", on_confirm)

    root.update()
    dialog.update()
    print("MODAL_OBSTACLE_READY", flush=True)

    # Auto-close after 25 seconds
    root.after(25000, root.destroy)
    root.mainloop()

if __name__ == "__main__":
    main()
