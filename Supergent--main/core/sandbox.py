import subprocess
import tempfile
import os

class Sandbox:
    def run(self, cmd: list, cwd: str, timeout: int = 30) -> str:
        bwrap_cmd = [
            "bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", cwd, cwd,
            "--chdir", cwd,
            "--unshare-net",
            "--"
        ] + cmd

        try:
            p = subprocess.run(
                bwrap_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False
            )
            return p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return "TIMEOUT"
        except Exception as e:
            return str(e)
