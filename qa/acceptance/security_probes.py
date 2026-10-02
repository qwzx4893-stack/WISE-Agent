"""Non-destructive reachability probes for high-severity static findings."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="wise-security-probe-") as runtime:
        os.environ["WISE_RUNTIME_ROOT"] = runtime
        os.environ["AGENT_OS_SKIP_FIRST_BOOT_CHECK"] = "1"
        (Path(runtime) / "workspace").mkdir(parents=True, exist_ok=True)

        from core.capability_router import CapabilityRouter
        from core.security.security_gate import SecurityContext, WindowsSecurityGate

        gate = WindowsSecurityGate(audit_log_path=Path(runtime) / "security-audit.jsonl")
        trusted_eval = gate.evaluate_action(
            "run_shell", {"command": "echo WISE_SHELL_PROBE"}, SecurityContext()
        )
        untrusted_eval = gate.evaluate_action(
            "run_shell",
            {"command": "echo WISE_UNTRUSTED_SHELL_PROBE"},
            SecurityContext(is_untrusted_content=True, taint_sources=["web"]),
        )
        open_app_eval = gate.evaluate_action(
            "open_app", {"app": "notepad.exe & echo chained"}, SecurityContext()
        )

        # Execute only a harmless echo through the same adapter exposed to the
        # capability network.  This proves reachability without changing state.
        shell_result = CapabilityRouter().execute(
            "run_shell", {"command": "echo WISE_SHELL_PROBE"}, session_id="qa"
        )
        report = {
            "run_shell_trusted_allowed": trusted_eval.allowed,
            "run_shell_trusted_tier": trusted_eval.tier.value,
            "run_shell_untrusted_allowed": untrusted_eval.allowed,
            "run_shell_untrusted_tier": untrusted_eval.tier.value,
            "open_app_metacharacters_allowed": open_app_eval.allowed,
            "open_app_tier": open_app_eval.tier.value,
            "shell_adapter_reachable": shell_result.success,
            "shell_adapter_output": str(shell_result.output).strip(),
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        unsafe = (
            report["run_shell_trusted_allowed"]
            or report["run_shell_untrusted_allowed"]
            or report["open_app_metacharacters_allowed"]
            or report["shell_adapter_reachable"]
        )
        return 1 if unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())
