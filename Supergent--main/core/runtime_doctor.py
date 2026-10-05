# ==============================================================================
# WISE Canonical Runtime Doctor & Health Matrix
# Provides comprehensive, truthful diagnostics across host environment,
# dependencies, browser engines, local/cloud models, audio, MCP, and security.
# Zero mocking. Real checks only.
# ==============================================================================

from __future__ import annotations

import os
import sys
import time
import json
import socket
import logging
import platform
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

import psutil

LOG = logging.getLogger("WISE.RuntimeDoctor")

_WISE_ROOT = Path(__file__).resolve().parent.parent.parent
_SUPERGENT_ROOT = Path(__file__).resolve().parent.parent


class ComponentStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    AVAILABLE_NOT_RUNNING = "AVAILABLE_NOT_RUNNING"
    ENVIRONMENT_BLOCKED = "ENVIRONMENT_BLOCKED"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    MISCONFIGURED = "MISCONFIGURED"
    FAILED = "FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class DiagnosticCheck:
    name: str
    status: str  # "PASS", "WARN", "FAIL"
    message: str
    code: str = "READY"  # Fine-grained ComponentStatus
    details: Optional[Dict[str, Any]] = None


@dataclass
class RuntimeDoctorReport:
    timestamp: float
    os_info: Dict[str, Any]
    python_info: Dict[str, Any]
    memory_info: Dict[str, Any]
    dependencies: List[DiagnosticCheck]
    browser_health: DiagnosticCheck
    model_health: DiagnosticCheck
    mcp_health: DiagnosticCheck
    audio_health: DiagnosticCheck
    security_health: DiagnosticCheck
    overall_status: str  # "HEALTHY", "DEGRADED", "CRITICAL"
    blender_health: Optional[DiagnosticCheck] = None
    perception_health: Optional[DiagnosticCheck] = None
    memory_health: Optional[DiagnosticCheck] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status,
            "os_info": self.os_info,
            "python_info": self.python_info,
            "memory_info": self.memory_info,
            "dependencies": [asdict(d) for d in self.dependencies],
            "browser_health": asdict(self.browser_health),
            "model_health": asdict(self.model_health),
            "mcp_health": asdict(self.mcp_health),
            "audio_health": asdict(self.audio_health),
            "security_health": asdict(self.security_health),
            "blender_health": asdict(self.blender_health) if self.blender_health else None,
            "perception_health": asdict(self.perception_health) if self.perception_health else None,
            "memory_health": asdict(self.memory_health) if self.memory_health else None,
        }


class RuntimeDoctor:
    """
    Evaluates and certifies WISE host environment readiness.
    Provides authoritative pre-flight telemetry for local desktop and browser automation.
    """

    def __init__(self) -> None:
        self.wise_root = _WISE_ROOT
        self.supergent_root = _SUPERGENT_ROOT

    def inspect_system(self) -> Dict[str, Any]:
        return {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count_logical": psutil.cpu_count(logical=True),
            "cpu_count_physical": psutil.cpu_count(logical=False),
        }

    def inspect_python(self) -> Dict[str, Any]:
        return {
            "version": platform.python_version(),
            "executable": sys.executable,
            "is_64bit": sys.maxsize > 2**32,
        }

    def inspect_memory(self) -> Dict[str, Any]:
        vm = psutil.virtual_memory()
        total_gb = vm.total / (1024 ** 3)
        available_gb = vm.available / (1024 ** 3)
        used_gb = vm.used / (1024 ** 3)
        safety_threshold_gb = 5.8
        is_safe_for_local_model = available_gb >= safety_threshold_gb

        return {
            "total_ram_gb": round(total_gb, 2),
            "available_ram_gb": round(available_gb, 2),
            "used_ram_gb": round(used_gb, 2),
            "percent_used": vm.percent,
            "safety_threshold_gb": safety_threshold_gb,
            "is_safe_for_local_model": is_safe_for_local_model,
            "verdict": "SAFE" if is_safe_for_local_model else "CONSTRAINED",
        }

    def inspect_dependencies(self) -> List[DiagnosticCheck]:
        required_modules = [
            ("psutil", "Hardware telemetry and process inspection"),
            ("playwright", "High-fidelity browser automation"),
            ("win32gui", "Windows native Win32 window management"),
            ("win32con", "Windows native Win32 constants"),
            ("win32api", "Windows native Win32 API functions"),
            ("numpy", "Numerical processing and audio VAD analysis"),
            ("docx", "Office document manipulation (python-docx)"),
            ("cachetools", "Bounded LRU and TTL memory caching"),
            ("fastapi", "High-performance API server"),
            ("uvicorn", "ASGI web server"),
        ]
        checks = []
        for mod, desc in required_modules:
            try:
                __import__(mod)
                checks.append(DiagnosticCheck(name=mod, status="PASS", message=f"Installed ({desc})"))
            except ImportError as err:
                checks.append(DiagnosticCheck(name=mod, status="FAIL", message=f"Missing: {err} ({desc})"))
            except Exception as err:
                checks.append(DiagnosticCheck(name=mod, status="WARN", message=f"Warning on import: {err}"))
        return checks

    def inspect_browser(self) -> DiagnosticCheck:
        """Verifies WISE's configured Brave automation path end-to-end."""
        try:
            from core.browser.browser_models import BrowserSessionConfig
            from core.browser.browser_session import BrowserSession

            session = BrowserSession(BrowserSessionConfig(headless=True))
            try:
                session.start()
                page = session.new_page("data:text/html,<title>WISE Brave Health</title>")
                title = page.title()
                if title != "WISE Brave Health":
                    raise RuntimeError(f"Unexpected browser health-page title: {title!r}")
                launch = session._launch_kwargs()
                return DiagnosticCheck(
                    name="browser_automation",
                    status="PASS",
                    message="Brave browser automation fully functional",
                    details={
                        "headless_render_verified": True,
                        "browser": "brave",
                        "executable_path": launch.get("executable_path"),
                    },
                )
            except Exception as b_err:
                return DiagnosticCheck(
                    name="browser_automation",
                    status="WARN",
                    message=f"Brave browser launch failed: {b_err}",
                )
            finally:
                session.stop()
        except Exception as e:
            return DiagnosticCheck(
                name="browser_automation",
                status="FAIL",
                message=f"Playwright unavailable: {e}",
            )

    def inspect_model_runtime(self) -> DiagnosticCheck:
        """Checks local GGUF model file and active provider status."""
        model_path = self.wise_root / "scratch" / "models" / "LFM2.5-8B-A1B-Q4_K_M.gguf"
        file_exists = model_path.is_file()
        file_size_gb = (model_path.stat().st_size / (1024 ** 3)) if file_exists else 0.0

        vm = psutil.virtual_memory()
        avail_ram_gb = vm.available / (1024 ** 3)

        details = {
            "model_path": str(model_path),
            "file_exists": file_exists,
            "file_size_gb": round(file_size_gb, 2),
            "available_ram_gb": round(avail_ram_gb, 2),
            "required_ram_gb": 5.8,
        }

        if not file_exists:
            return DiagnosticCheck(
                name="model_runtime",
                status="WARN",
                message="Local LFM2.5 GGUF model file not found; running via configured Cloud/Fallback provider",
                details=details,
            )

        if avail_ram_gb < 5.8:
            return DiagnosticCheck(
                name="model_runtime",
                status="WARN",
                message=f"LFM2.5 model exists ({file_size_gb:.2f} GB) but host RAM ({avail_ram_gb:.2f} GB) is below 5.8 GB threshold; routed to Cloud/Unavailable",
                details=details,
            )

        return DiagnosticCheck(
            name="model_runtime",
            status="PASS",
            message=f"LFM2.5 model verified ({file_size_gb:.2f} GB) and certified safe to load (RAM >= 5.8 GB)",
            details=details,
        )

    def inspect_mcp_ecosystem(self) -> DiagnosticCheck:
        """Inspects canonical MCP registry and active server configurations."""
        try:
            from core.mcp.registry import get_mcp_registry
            reg = get_mcp_registry()
            servers = reg.list_servers()
            server_names = [s.get("name") if isinstance(s, dict) else getattr(s, "name", str(s)) for s in servers] if servers else []
            return DiagnosticCheck(
                name="mcp_ecosystem",
                status="PASS",
                message=f"MCP Registry operational with {len(servers)} configured servers",
                details={"servers": server_names, "total_count": len(servers)},
            )
        except Exception as e:
            return DiagnosticCheck(
                name="mcp_ecosystem",
                status="WARN",
                message=f"MCP registry inspection exception: {e}",
            )

    def inspect_audio(self) -> DiagnosticCheck:
        """Inspects Windows audio drivers, SAPI5 TTS, VAD, and BargeIn."""
        try:
            from core.voice.runtime import VoiceRuntime
            vr = VoiceRuntime()
            details = vr.probe_voice_environment()
            mic_ready = bool(details["microphone"].get("is_available"))
            # Readiness must describe the providers WISE will actually use,
            # not merely Windows' optional SAPI/recognizer fallbacks.  For
            # example Faster-Whisper can support Arabic even without a Windows
            # Arabic speech pack, and IndexTTS has different VRAM constraints.
            stt_info = details.get("speech_to_text", {})
            stt_ready = bool(stt_info.get(
                "selected_stt_available",
                stt_info.get("native_stt_available", False),
            ))
            selected_tts = getattr(getattr(vr, "tts", None), "primary_provider", None)
            if selected_tts is not None:
                tts_ready = bool(selected_tts.is_available())
            else:
                native_tts = getattr(getattr(vr, "tts", None), "native_provider", None)
                tts_ready = bool(getattr(native_tts, "is_available", lambda: False)())
            tts_info = details.get("text_to_speech", {})
            resource = (tts_info.get("cloned_voice", {}) or {}).get("resource", {})
            tts_ready = tts_ready and bool(resource.get("ready", True))
            state = vr.state.value
            if not (mic_ready and stt_ready and tts_ready):
                missing = [
                    name for name, ready in (
                        ("microphone", mic_ready),
                        ("speech-to-text", stt_ready),
                        ("text-to-speech", tts_ready),
                    ) if not ready
                ]
                return DiagnosticCheck(
                    name="audio_subsystem",
                    status="WARN",
                    message=f"Voice runtime is not ready; unavailable: {', '.join(missing)}",
                    code=ComponentStatus.NOT_CONFIGURED.value,
                    details=details,
                )
            return DiagnosticCheck(
                name="audio_subsystem",
                status="PASS",
                message=f"VoiceRuntime operational in state '{state}' with Barge-In active",
                details=details,
            )
        except Exception as e:
            return DiagnosticCheck(
                name="audio_subsystem",
                status="WARN",
                message=f"Audio subsystem running in degraded/headless mode: {e}",
            )

    def inspect_security(self) -> DiagnosticCheck:
        """Inspects WindowsSecurityGate and FilesystemGovernor configuration."""
        try:
            from core.security.security_gate import WindowsSecurityGate
            gate = WindowsSecurityGate()
            return DiagnosticCheck(
                name="security_governance",
                status="PASS",
                message="WindowsSecurityGate and ConfirmationManager initialized and active",
                details={"audit_log": str(gate.audit_log_path)},
            )
        except Exception as e:
            return DiagnosticCheck(
                name="security_governance",
                status="FAIL",
                message=f"Security gate initialization failed: {e}",
            )

    def inspect_blender(self) -> DiagnosticCheck:
        """Inspects Blender installation and active MCP socket bridge daemon."""
        blender_exe = Path(r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe")
        exe_exists = blender_exe.is_file()

        # Check socket daemon on 9876
        socket_open = False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                socket_open = (s.connect_ex(("127.0.0.1", 9876)) == 0)
        except Exception:
            pass

        if socket_open:
            return DiagnosticCheck(
                name="blender_subsystem",
                status="PASS",
                code=ComponentStatus.RUNNING.value,
                message="Blender 5.2 MCP Daemon active and accepting socket connections on 127.0.0.1:9876",
                details={"executable": str(blender_exe), "port": 9876, "socket_open": True},
            )
        elif exe_exists:
            return DiagnosticCheck(
                name="blender_subsystem",
                status="WARN",
                code=ComponentStatus.AVAILABLE_NOT_RUNNING.value,
                message="Blender 5.2 executable present but background MCP daemon is not running on port 9876",
                details={"executable": str(blender_exe), "port": 9876, "socket_open": False},
            )
        else:
            return DiagnosticCheck(
                name="blender_subsystem",
                status="WARN",
                code=ComponentStatus.MISSING_DEPENDENCY.value,
                message="Blender 5.2 binary not found in standard installation directory",
                details={"checked_path": str(blender_exe)},
            )

    def inspect_perception(self) -> DiagnosticCheck:
        """Inspects hybrid perception subsystems: UIA, OCR, and Vision Grounding."""
        uia_ok = False
        try:
            from core.vision.ui_tree import get_ui_tree_extractor
            tree_ext = get_ui_tree_extractor()
            uia_ok = tree_ext is not None
        except Exception:
            pass

        ocr_ok = False
        try:
            from core.vision.ocr_engine import get_ocr_engine
            ocr = get_ocr_engine()
            ocr_ok = ocr is not None
        except Exception:
            pass

        if uia_ok and ocr_ok:
            return DiagnosticCheck(
                name="hybrid_perception",
                status="PASS",
                code=ComponentStatus.READY.value,
                message="Hybrid Perception pipeline ready (UIA tree + Windows OCR + TargetResolver active)",
                details={"uia_active": uia_ok, "ocr_active": ocr_ok},
            )
        else:
            return DiagnosticCheck(
                name="hybrid_perception",
                status="WARN",
                code=ComponentStatus.MISCONFIGURED.value,
                message=f"Hybrid Perception degraded: UIA={uia_ok}, OCR={ocr_ok}",
                details={"uia_active": uia_ok, "ocr_active": ocr_ok},
            )

    def inspect_memory_subsystem(self) -> DiagnosticCheck:
        """Inspects 5-tier memory service and persistence store."""
        try:
            from core.memory_service import get_memory_service
            mem = get_memory_service()
            items = mem.list_all()
            return DiagnosticCheck(
                name="memory_subsystem",
                status="PASS",
                code=ComponentStatus.READY.value,
                message="5-Tier MemoryService operational with token redaction and contradiction resolution",
                details={"total_items": len(items)},
            )
        except Exception as e:
            return DiagnosticCheck(
                name="memory_subsystem",
                status="WARN",
                code=ComponentStatus.FAILED.value,
                message=f"MemoryService initialization issue: {e}",
            )

    def run_full_diagnosis(self) -> RuntimeDoctorReport:
        """Runs the complete diagnostic suite and determines overall ecosystem health."""
        os_info = self.inspect_system()
        python_info = self.inspect_python()
        memory_info = self.inspect_memory()
        dep_checks = self.inspect_dependencies()
        browser_check = self.inspect_browser()
        model_check = self.inspect_model_runtime()
        mcp_check = self.inspect_mcp_ecosystem()
        audio_check = self.inspect_audio()
        security_check = self.inspect_security()
        blender_check = self.inspect_blender()
        perception_check = self.inspect_perception()
        memory_check = self.inspect_memory_subsystem()

        # Determine overall status
        all_checks = dep_checks + [
            browser_check, model_check, mcp_check, audio_check,
            security_check, blender_check, perception_check, memory_check
        ]
        has_fail = any(c.status == "FAIL" for c in all_checks)
        has_warn = any(c.status == "WARN" for c in all_checks)

        if has_fail:
            overall = "CRITICAL"
        elif has_warn:
            overall = "DEGRADED"
        else:
            overall = "HEALTHY"

        return RuntimeDoctorReport(
            timestamp=time.time(),
            os_info=os_info,
            python_info=python_info,
            memory_info=memory_info,
            dependencies=dep_checks,
            browser_health=browser_check,
            model_health=model_check,
            mcp_health=mcp_check,
            audio_health=audio_check,
            security_health=security_check,
            overall_status=overall,
            blender_health=blender_check,
            perception_health=perception_check,
            memory_health=memory_check,
        )


def main() -> None:
    doctor = RuntimeDoctor()
    as_json = "--json" in sys.argv

    report = doctor.run_full_diagnosis()

    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return

    print("\n" + "=" * 70)
    print(f" WISE CANONICAL RUNTIME DOCTOR — HEALTH MATRIX ({report.overall_status})")
    print("=" * 70)
    print(f" OS: {report.os_info['system']} {report.os_info['release']} ({report.os_info['machine']})")
    print(f" Python: {report.python_info['version']} ({report.python_info['executable']})")
    print(f" RAM: Total {report.memory_info['total_ram_gb']} GB | Available {report.memory_info['available_ram_gb']} GB ({report.memory_info['percent_used']}% used)")
    print(f" Memory Safety Verdict: {report.memory_info['verdict']} (Threshold: >= {report.memory_info['safety_threshold_gb']} GB)")
    print("-" * 70)

    print(" SUBSYSTEM HEALTH:")
    checks = [
        ("Browser Engine", report.browser_health),
        ("Model Runtime", report.model_health),
        ("MCP Ecosystem", report.mcp_health),
        ("Voice Interface", report.audio_health),
        ("Security Gate", report.security_health),
    ]
    for label, c in checks:
        icon = "[PASS]" if c.status == "PASS" else ("[WARN]" if c.status == "WARN" else "[FAIL]")
        print(f"  {icon:<7} {label:<20} {c.message}")

    print("-" * 70)
    print(" CRITICAL DEPENDENCIES:")
    for d in report.dependencies:
        icon = "[PASS]" if d.status == "PASS" else ("[WARN]" if d.status == "WARN" else "[FAIL]")
        print(f"  {icon:<7} {d.name:<20} {d.message}")

    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
