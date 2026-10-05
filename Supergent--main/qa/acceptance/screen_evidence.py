"""Reject missing/blank native captures, without pretending pixels prove UX."""
from pathlib import Path


def inspect_native_record(record: dict) -> list[str]:
    """Validate pixels and, for viewport previews, the launched native identity.

    Legacy desktop GDI records remain under their original schema; they never
    become viewport evidence merely by pointing at a different PNG.
    """
    failures = []
    path = Path(record.get("screenshot") or "")
    capture = inspect_native_frame(path)
    if capture["verdict"] != "NOT_BLANK":
        failures.append(capture["reason"])
    if record.get("renderer_capture") is not None or record.get("screenshot_scope"):
        from qa.acceptance.native_renderer_host import SCOPE, verify_renderer_evidence
        try:
            if record.get("screenshot_scope") != SCOPE:
                raise ValueError("Native viewport screenshot scope is missing or inconsistent")
            if not isinstance(record.get("renderer_capture"), dict):
                raise ValueError("Native viewport capture metadata is missing or malformed")
            pid, hwnd = record["window_pid"], record["window_handle"]
            if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                    or isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0
                    or pid not in record["owned_process_ids"]):
                raise ValueError("Native window does not belong to the recorded launched process family")
            verify_renderer_evidence(record["renderer_capture"], image_path=path,
                hwnd=hwnd, pid=pid, runtime=record["runtime_root"], url=record["runtime_url"])
            if record.get("native_acrylic_frame_status") != "UNVERIFIED":
                raise ValueError("Renderer preview cannot certify the native Acrylic frame")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            failures.append("Invalid owned native renderer provenance: " + str(exc))
    return failures


def inspect_native_frame(path: Path) -> dict:
    from PIL import Image

    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
            if width < 640 or height < 480:
                return {"verdict": "FAIL", "reason": "Native frame is too small to inspect", "size": [width, height]}
            # Title bar and native borders can be visible even when the actual
            # WebView body was not captured. Do not let them make a blank pass.
            body = image.convert("RGB").crop((24, 72, width - 24, height - 24))
            sample = body.resize((256, 160)).quantize(colors=64)
            counts = sample.getcolors() or []
            dominant = max((count for count, _ in counts), default=0) / (256 * 160)
            nonblank = len(counts) >= 8 and dominant < .995
            return {"verdict": "NOT_BLANK" if nonblank else "FAIL", "size": [width, height],
                    "body_color_bins": len(counts), "dominant_body_fraction": round(dominant, 6),
                    "reason": "Nonblank capture; semantic visual review still required" if nonblank else
                              "Native content is blank or effectively uniform; UIA success is not visual evidence"}
    except (OSError, ValueError) as exc:
        return {"verdict": "FAIL", "reason": "Native screenshot cannot be decoded: " + type(exc).__name__}
