# Local security tools on demand

The main runtime does not require Bandit, detect-secrets, YARA or stix2. Their
reviewed adapters obtain a lease from `core/security/optional_tools.py` and
install fixed recipes into `WISE_RUNTIME_ROOT/optional-security-tools` only
when an operation actually needs them. Workers use that environment's Python,
not the application/system installation. No arbitrary tool name or pip flags
can be supplied by a model.

Dependencies are binary wheels from PyPI; every downloaded wheel is compared
with the SHA256 published by PyPI before installation. Provenance, resolved
dependency versions and last-use time are kept in a non-secret manifest. This
integrity check is not an audit of upstream code or a full OS sandbox.

`WISE_SECURITY_TOOL_TTL_SECONDS=3600` retains managed environments for one idle
hour to avoid repeatedly downloading them during a task. `0` removes an
environment after its lease ends. Housekeeping runs when managed tools are
used or their status is queried; it is not an independently running system
service. An explicit `OptionalToolManager.remove(id)` removes only the exact
reviewed WISE cache directory and refuses unknown names or symlink escapes.
Leases use a cross-process lock so an active tool cannot be removed.

Download, integrity or worker errors are actual failures. Other catalog tools
without a reviewed recipe and adapter remain unavailable; WISE does not
pretend to dynamically support every security program. Existing packages in
the user's global Python are not removed. This change governs WISE's own
runtime and future installs, not unrelated software installed on the computer.
