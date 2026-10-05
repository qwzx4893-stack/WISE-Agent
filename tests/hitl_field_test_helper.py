#!/usr/bin/env python3
"""
WISE HITL Real Windows Field Test Helper
Spawns a real native Windows GUI window presenting a controlled human verification
challenge to test real UIA/OCR challenge detection, task pausing, human intervention,
fresh observation, and resume.
"""

import sys
import time
import ctypes
import threading

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
    root.title("WISE_HITL_VERIFICATION_TEST")
    root.geometry("450x300+250+250")
    root.configure(bg="#1E1E2E")

    header = tk.Label(
        root,
        text="Security Check: Verify you are human",
        fg="#F38BA8",
        bg="#1E1E2E",
        font=("Segoe UI", 13, "bold"),
    )
    header.pack(pady=20)

    sub_label = tk.Label(
        root,
        text="Human verification required. Anti-bot protection active.",
        fg="#CDD6F4",
        bg="#1E1E2E",
        font=("Segoe UI", 10),
    )
    sub_label.pack(pady=10)

    status_var = tk.StringVar(value="Status: CHALLENGE_ACTIVE")
    status_label = tk.Label(
        root,
        textvariable=status_var,
        fg="#FAB387",
        bg="#1E1E2E",
        font=("Consolas", 10, "bold"),
    )
    status_label.pack(pady=10)

    def on_solve(event=None):
        status_var.set("Status: VERIFIED_ACCESS_GRANTED")
        header.config(text="Access Granted: Challenge Solved", fg="#A6E3A1")
        sub_label.config(text="Human verification completed successfully.")
        root.title("WISE_HITL_ACCESS_GRANTED")
        print("CHALLENGE_SOLVED_BY_HUMAN", flush=True)
        root.update()

    btn = tk.Button(
        root,
        text="I am not a robot (Solve Challenge)",
        command=on_solve,
        bg="#89B4FA",
        fg="#11111B",
        font=("Segoe UI", 11, "bold"),
        padx=15,
        pady=8,
    )
    btn.pack(pady=20)
    btn.focus_set()

    # Keyboard triggers
    root.bind("<Return>", on_solve)
    root.bind("<space>", on_solve)
    btn.bind("<Return>", on_solve)
    btn.bind("<space>", on_solve)

    # Background thread listening to stdin pipe
    def listen_stdin():
        try:
            for line in sys.stdin:
                if "SOLVE" in line.strip().upper():
                    root.after(0, on_solve)
                    break
        except Exception:
            pass

    t = threading.Thread(target=listen_stdin, daemon=True)
    t.start()

    root.update()
    print("CHALLENGE_WINDOW_READY", flush=True)

    # Auto close after 35 seconds
    root.after(35000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
