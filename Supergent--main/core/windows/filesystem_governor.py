"""
Windows Filesystem Governor (FilesystemGovernor).
Enforces fine-grained filesystem boundaries, path canonicalization, traversal attack prevention,
and directory permission tiers outside the LLM.
Seamlessly integrates with WindowsSecurityGate to protect sensitive OS directories while granting
full, reliable access to user workspaces and data folders.
"""

from __future__ import annotations

import os
import sys
import logging
import threading
from pathlib import Path
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from core.security.security_gate import ActionTier

LOG = logging.getLogger("wise.filesystem_governor")


class FileOperation(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    DELETE = "DELETE"
    MOVE = "MOVE"
    LIST = "LIST"


@dataclass
class PolicyDecision:
    allowed: bool
    tier: ActionTier
    canonical_path: str
    reason: str
    requires_confirmation: bool = False
    is_protected_system: bool = False


class FilesystemGovernor:
    """Rigorous filesystem boundary enforcer and security filter."""

    def __init__(self, workspace_root: Optional[Path] = None):
        self._lock = threading.RLock()
        
        # Determine workspace root
        if workspace_root is None:
            # Default to repository root
            self.workspace_root = Path(__file__).resolve().parent.parent.parent
        else:
            self.workspace_root = Path(workspace_root).resolve()

        self._user_home = Path.home().resolve()
        
        # Whitelisted user domains (Relative freedom for intelligent desktop assistance)
        self._whitelisted_roots: List[Path] = [
            self.workspace_root,
            self._user_home / "Desktop",
            self._user_home / "Downloads",
            self._user_home / "Documents",
            self._user_home / "Pictures",
            self._user_home / "Videos",
            self._user_home / "Music",
            self._user_home / "AppData" / "Local" / "Temp",
        ]

        # Protected system directories (Require high privilege / interactive confirmation or blocked)
        self._protected_system_roots: List[str] = [
            r"c:\windows",
            r"c:\program files",
            r"c:\program files (x86)",
            r"c:\boot",
            r"c:\recovery",
            r"c:\system volume information",
            r"c:\pagefile.sys",
            r"c:\swapfile.sys",
            r"c:\hiberfil.sys",
        ]

        # Sensitive credential and key paths (Strictly protected even from user writes)
        self._sensitive_credential_roots: List[str] = [
            os.path.normcase(str(self._user_home / ".ssh")),
            os.path.normcase(str(self._user_home / ".gnupg")),
            os.path.normcase(str(self._user_home / "AppData" / "Local" / "Microsoft" / "Credentials")),
            os.path.normcase(str(self._user_home / "AppData" / "Roaming" / "Microsoft" / "Crypto")),
            os.path.normcase(str(self._user_home / "AppData" / "Roaming" / "Microsoft" / "Vault")),
        ]

    def add_whitelisted_root(self, path: Path | str) -> None:
        with self._lock:
            resolved = Path(path).resolve()
            if resolved not in self._whitelisted_roots:
                self._whitelisted_roots.append(resolved)
                LOG.info("Added whitelisted filesystem domain: %s", resolved)

    def canonicalize_path(self, raw_path: str | Path) -> str:
        """Resolves symlinks, relative segments, and canonicalizes casing for Windows."""
        try:
            # Realpath resolves symlinks and junctions to defeat path traversal bypasses
            norm = os.path.realpath(os.path.abspath(str(raw_path)))
            return os.path.normcase(norm)
        except Exception as e:
            LOG.warning("Failed to canonicalize path '%s': %s", raw_path, e)
            return os.path.normcase(os.path.abspath(str(raw_path)))

    def is_in_system_directory(self, canonical_path: str) -> bool:
        for sys_dir in self._protected_system_roots:
            if canonical_path == sys_dir or canonical_path.startswith(sys_dir + os.sep):
                return True
        return False

    def is_in_sensitive_credentials(self, canonical_path: str) -> bool:
        for cred_dir in self._sensitive_credential_roots:
            if canonical_path == cred_dir or canonical_path.startswith(cred_dir + os.sep):
                return True
        return False

    def is_in_whitelisted_user_domain(self, canonical_path: str) -> bool:
        for white_dir in self._whitelisted_roots:
            white_norm = os.path.normcase(str(white_dir))
            if canonical_path == white_norm or canonical_path.startswith(white_norm + os.sep):
                return True
        return False

    def evaluate_operation(
        self,
        op: FileOperation,
        path: str | Path,
        secondary_path: Optional[str | Path] = None,
        is_confirmed: bool = False,
    ) -> PolicyDecision:
        """Evaluates an intended file operation and returns a PolicyDecision with appropriate tier."""
        c_path = self.canonicalize_path(path)

        # 1. Block access to sensitive user credentials and cryptographic keys
        if self.is_in_sensitive_credentials(c_path):
            if op in (FileOperation.WRITE, FileOperation.DELETE, FileOperation.MOVE):
                return PolicyDecision(
                    allowed=False,
                    tier=ActionTier.DESTRUCTIVE,
                    canonical_path=c_path,
                    reason=f"Access denied: Path '{c_path}' targets sensitive credentials/crypto keys.",
                    requires_confirmation=False,
                    is_protected_system=True,
                )
            elif op in (FileOperation.READ, FileOperation.LIST):
                return PolicyDecision(
                    allowed=False,
                    tier=ActionTier.ADMINISTRATIVE,
                    canonical_path=c_path,
                    reason=f"Read blocked: Path '{c_path}' targets sensitive credential store.",
                    requires_confirmation=False,
                    is_protected_system=True,
                )

        # 2. Check Protected System Directories
        if self.is_in_system_directory(c_path):
            if op == FileOperation.READ or op == FileOperation.LIST:
                # System read is allowed with logging
                return PolicyDecision(
                    allowed=True,
                    tier=ActionTier.READ,
                    canonical_path=c_path,
                    reason="System read permitted.",
                    is_protected_system=True,
                )
            else:
                # Write/Delete/Move in system directory is DESTRUCTIVE and requires explicit confirmation
                if is_confirmed:
                    return PolicyDecision(
                        allowed=True,
                        tier=ActionTier.DESTRUCTIVE,
                        canonical_path=c_path,
                        reason="Confirmed administrative modification in system directory.",
                        is_protected_system=True,
                    )
                else:
                    return PolicyDecision(
                        allowed=False,
                        tier=ActionTier.DESTRUCTIVE,
                        canonical_path=c_path,
                        reason=f"System directory modification '{c_path}' requires explicit user confirmation.",
                        requires_confirmation=True,
                        is_protected_system=True,
                    )

        # 3. Handle secondary path for MOVE operations
        if op == FileOperation.MOVE and secondary_path:
            c_target = self.canonicalize_path(secondary_path)
            target_decision = self.evaluate_operation(FileOperation.WRITE, c_target, is_confirmed=is_confirmed)
            if not target_decision.allowed:
                return target_decision

        # 4. Standard User Domain Evaluation
        if self.is_in_whitelisted_user_domain(c_path):
            if op == FileOperation.READ or op == FileOperation.LIST:
                return PolicyDecision(
                    allowed=True,
                    tier=ActionTier.READ,
                    canonical_path=c_path,
                    reason="Whitelisted user domain read allowed.",
                )
            elif op == FileOperation.WRITE or op == FileOperation.MOVE:
                return PolicyDecision(
                    allowed=True,
                    tier=ActionTier.LOW_RISK,
                    canonical_path=c_path,
                    reason="Whitelisted user domain write allowed.",
                )
            elif op == FileOperation.DELETE:
                # Workspace deletes are moderate, Personal user folder deletes (e.g. Documents) require confirmation
                is_workspace = c_path.startswith(os.path.normcase(str(self.workspace_root)))
                if is_workspace:
                    return PolicyDecision(
                        allowed=True,
                        tier=ActionTier.MODERATE,
                        canonical_path=c_path,
                        reason="Workspace delete allowed with logging.",
                    )
                else:
                    if is_confirmed:
                        return PolicyDecision(
                            allowed=True,
                            tier=ActionTier.MODERATE,
                            canonical_path=c_path,
                            reason="Confirmed user file deletion allowed.",
                        )
                    return PolicyDecision(
                        allowed=False,
                        tier=ActionTier.MODERATE,
                        canonical_path=c_path,
                        reason="Personal user file deletion requires confirmation.",
                        requires_confirmation=True,
                    )

        # 5. Non-whitelisted general path (e.g. secondary drives or external paths)
        if op == FileOperation.READ or op == FileOperation.LIST:
            return PolicyDecision(
                allowed=True,
                tier=ActionTier.READ,
                canonical_path=c_path,
                reason="General filesystem read allowed.",
            )
        elif op == FileOperation.WRITE or op == FileOperation.MOVE:
            return PolicyDecision(
                allowed=True,
                tier=ActionTier.MODERATE,
                canonical_path=c_path,
                reason="General filesystem write allowed with audit logging.",
            )
        else: # DELETE
            if is_confirmed:
                return PolicyDecision(
                    allowed=True,
                    tier=ActionTier.MODERATE,
                    canonical_path=c_path,
                    reason="Confirmed general file delete allowed.",
                )
            return PolicyDecision(
                allowed=False,
                tier=ActionTier.MODERATE,
                canonical_path=c_path,
                reason="General file deletion outside workspace requires confirmation.",
                requires_confirmation=True,
            )


# Singleton instance
_GLOBAL_FS_GOVERNOR: Optional[FilesystemGovernor] = None
_FSG_LOCK = threading.Lock()


def get_filesystem_governor() -> FilesystemGovernor:
    global _GLOBAL_FS_GOVERNOR
    if _GLOBAL_FS_GOVERNOR is None:
        with _FSG_LOCK:
            if _GLOBAL_FS_GOVERNOR is None:
                _GLOBAL_FS_GOVERNOR = FilesystemGovernor()
    return _GLOBAL_FS_GOVERNOR
