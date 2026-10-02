import os
import shutil
import subprocess
try:
    import resource
except ImportError:
    resource = None
from typing import List
from .seccomp import SeccompManager

class SandboxManager:
    """Sandbox runner. Prefers bwrap, falls back to plain subprocess if absent."""

    def __init__(self):
        self.use_bwrap = shutil.which("bwrap") is not None
        self.bwrap_supports_seccomp = self._verify_bwrap_seccomp() if self.use_bwrap else False
        self.available = self.use_bwrap

    def _verify_bwrap_seccomp(self) -> bool:
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as f:
                f.write('{"defaultAction":"SCMP_ACT_ERRNO","architectures":["SCMP_ARCH_X86_64"],"syscalls":[]}')
                f.flush()
                proc = subprocess.run(["bwrap", "--seccomp", f.name, "--", "true"], capture_output=True, timeout=5)
                return proc.returncode == 0
        except:
            return False

    def run(self, cmd: List[str], cwd: str, requires_network: bool,
            timeout: int, cpu_limit: int = None, memory_limit: str = None) -> str:
        if self.use_bwrap:
            return self._run_bwrap(cmd, cwd, requires_network, timeout, cpu_limit, memory_limit)
        return self._run_plain(cmd, cwd, timeout, cpu_limit, memory_limit)

    def _run_plain(self, cmd: List[str], cwd: str, timeout: int,
                   cpu_limit: int, memory_limit: str) -> str:
        """Plain subprocess fallback when bwrap is unavailable."""
        return self._run_with_limits(cmd, timeout, cpu_limit, memory_limit, cwd=cwd)

    def _apply_limits(self, cpu_limit: int = None, memory_limit: str = None):
        if resource is None:
            return
        if cpu_limit:
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            except:
                pass
        if memory_limit:
            try:
                mem = self._parse_memory(memory_limit)
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except:
                pass

    def _parse_memory(self, mem_str: str) -> int:
        mem_str = mem_str.upper()
        if mem_str.endswith("K"):
            return int(mem_str[:-1]) * 1024
        elif mem_str.endswith("M"):
            return int(mem_str[:-1]) * 1024 * 1024
        elif mem_str.endswith("G"):
            return int(mem_str[:-1]) * 1024 * 1024 * 1024
        return int(mem_str)

    def _run_bwrap(self, cmd: List[str], cwd: str, requires_network: bool, 
                   timeout: int, cpu_limit: int, memory_limit: str) -> str:
        bwrap_cmd = [
            "bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", cwd, cwd,
            "--chdir", cwd,
        ]
        # Bind FS roots only when they exist (minimal containers included).
        for ro in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
            if os.path.exists(ro):
                bwrap_cmd[4:4] = ["--ro-bind", ro, ro]
        if self.bwrap_supports_seccomp:
            seccomp_path = SeccompManager.get_profile_path()
            bwrap_cmd.extend(["--seccomp", seccomp_path])
        if not requires_network:
            bwrap_cmd.append("--unshare-net")
        bwrap_cmd.append("--")
        bwrap_cmd.extend(cmd)

        return self._run_with_limits(bwrap_cmd, timeout, cpu_limit, memory_limit)

    def _run_with_limits(self, cmd: List[str], timeout: int,
                         cpu_limit: int, memory_limit: str, cwd: str = None) -> str:
        def preexec():
            self._apply_limits(cpu_limit, memory_limit)
            try:
                os.setsid()
            except OSError:
                pass
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                preexec_fn=preexec if os.name != "nt" else None,
            )
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return "TIMEOUT"
        except Exception as e:
            return str(e)
