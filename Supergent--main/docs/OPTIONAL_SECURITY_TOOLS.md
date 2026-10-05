# Local security tools on demand

The main runtime does not require Bandit, detect-secrets, YARA, stix2 or pySigma. Their
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

## Sigma's actual scope

`intelligence.sigma` leases `pysigma==1.5.1` to parse an authorized workspace
YAML file in a separate offline worker. Input is bounded to 256 KB, 32 documents,
20 levels and 10,000 YAML nodes; aliases, duplicate keys, custom object tags,
collection/correlation rules and unsupported modifiers are rejected. Conditions
are actually parsed, including selector references; no SIEM, event-log matching,
conversion backend, pipeline or external plugin is invoked. A 15-second worker
deadline and bounded output stop stalled parsing.

`success=true` means the parser operation completed. Inspect `valid` and each
record's diagnostics separately: a successfully inspected invalid rule is NOT a
valid detection. Even `valid=true` certifies only this bounded syntax/structure
inspection, not detection effectiveness or full Sigma specification compliance.
Rule bodies and exception values are not returned. Python socket blocking and
credential-stripped workers are not a general operating-system sandbox.
