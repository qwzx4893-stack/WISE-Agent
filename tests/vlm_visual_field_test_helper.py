#!/usr/bin/env python3
"""
WISE Phase P1.3-B: VLM Visual Field Test Helper
Renders a custom Tkinter window with pure canvas vectors:
- Non-UIA vector icon button (Play glyph with NO OCR-readable text)
- Status indicator light (starts RED, turns GREEN on click)
- Mini-bar chart visualization
- Bottom status/error banner
This forces WISE perception to escalate past UIA (0 elements) and OCR (no button text)
to Level 4 VLM for visual grounding and visual diff verification.
"""

import sys
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
    root.title("WISE_VLM_FIELD_TEST")
    root.geometry("500x380+250+250")
    root.configure(bg="#0F172A")

    canvas = tk.Canvas(root, width=500, height=380, bg="#0F172A", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    # 1. Header Toolbar
    canvas.create_rectangle(0, 0, 500, 50, fill="#1E293B", outline="")
    canvas.create_text(250, 25, text="WISE Visual Perception Testbed", font=("Segoe UI", 12, "bold"), fill="#F8FAFC")

    # 2. Status Indicator Light (RED initially)
    status_bulb = canvas.create_oval(450, 15, 475, 40, fill="#EF4444", outline="#DC2626", width=2)

    # 3. Custom Canvas Vector Icon: "Play Icon" (Coordinates: 120, 90 to 220, 190)
    # Pure vector geometry: circle + triangle (NO text inside the button!)
    btn_bg = canvas.create_oval(120, 90, 220, 190, fill="#3B82F6", outline="#60A5FA", width=3)
    # Triangle pointing right
    btn_glyph = canvas.create_polygon(155, 120, 155, 160, 195, 140, fill="#FFFFFF", outline="")

    # 4. Canvas Chart Visualization (3 vertical bars)
    canvas.create_rectangle(270, 90, 460, 240, fill="#1E293B", outline="#334155", width=1)
    canvas.create_rectangle(290, 180, 330, 230, fill="#38BDF8", outline="")
    canvas.create_rectangle(350, 140, 390, 230, fill="#818CF8", outline="")
    canvas.create_rectangle(410, 110, 450, 230, fill="#C084FC", outline="")

    # 5. Bottom Status Banner
    banner = canvas.create_rectangle(20, 290, 480, 350, fill="#334155", outline="#475569")
    banner_text = canvas.create_text(250, 320, text="System State: Awaiting Visual Interaction", font=("Segoe UI", 10), fill="#94A3B8")

    clicked_count = 0

    def on_click(event):
        nonlocal clicked_count
        # Check if clicked inside Play Icon circle (120, 90 to 220, 190)
        if 120 <= event.x <= 220 and 90 <= event.y <= 190:
            clicked_count += 1
            # Transform visual state: Turn Play Icon to Green with Checkmark
            canvas.itemconfig(btn_bg, fill="#10B981", outline="#34D399")
            canvas.delete(btn_glyph)
            canvas.create_line(150, 140, 165, 155, fill="#FFFFFF", width=4)
            canvas.create_line(165, 155, 195, 125, fill="#FFFFFF", width=4)

            # Turn Status bulb to Green
            canvas.itemconfig(status_bulb, fill="#10B981", outline="#059669")

            # Update Banner
            canvas.itemconfig(banner, fill="#065F46", outline="#047857")
            canvas.itemconfig(banner_text, text=f"SUCCESS: Play Vector Icon Activated ({clicked_count})", fill="#A7F3D0")

            print(f"VLM_ICON_CLICKED_AT_{event.x}_{event.y}_COUNT_{clicked_count}", flush=True)

    canvas.bind("<Button-1>", on_click)

    # Signal ready to test runner
    print("VLM_WINDOW_READY", flush=True)

    # Auto-destroy after 30 seconds if not closed
    root.after(30000, root.destroy)
    root.mainloop()

if __name__ == "__main__":
    main()
