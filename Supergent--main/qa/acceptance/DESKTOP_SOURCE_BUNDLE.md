# Local desktop source bundle contract

`package_desktop_source.py` creates a new local ZIP exactly once. It is source
for an installed Python environment, not an EXE, installer, portable application,
or verified clean-machine deployment. It does not publish the archive. The
historical pre-Plugins publication and its scripts remain independent.

The application stamp covers the frozen application's source. The v2 manifest
additionally lists every packaged source/support file with byte length and
SHA-256, and hashes the canonical sorted records. Retain the returned
`archive_sha256` outside the ZIP. Verification without a retained checksum only
checks internal consistency; an attacker can replace files and recompute an
internal manifest. No signature or authorship claim is made.

After the release owner authorizes sealing:

```powershell
python qa/acceptance/package_desktop_source.py --output C:\release\WISE-source.zip
python qa/acceptance/verify_desktop_source.py C:\release\WISE-source.zip --sha256 <retained-archive-sha256>
```

The verifier never extracts or executes content. It rejects duplicate names
(including Windows case collisions), traversal/absolute/ambiguous Windows
paths, symlink or non-regular entries, encryption, extra or missing members,
checksum/size changes, and state paths. Its limits are 50,000 files, 16 MB per
source file, 16 MB for the manifest, 32 MB for the central directory, and
512 MB total source bytes. ZIP64/multipart archives and wrapped/trailing payloads
are unsupported. Central directory member counting occurs before allocating
ZIP member objects. This is
tamper detection and bounded packaging verification, not a malware scan.

Source selection includes application modules (including `core/models`), UI
static assets, tool pack metadata, policies including `.rego`, all root
requirement lists, safe config defaults/example, sandbox bootstrap/catalog,
voice bootstrap/STT requirements, operational docs, QA source and tests plus
the pinned QA Python/Node requirement lists and toolchain guide.
Skill JavaScript modules, XML document templates, SQL/OPA assets and text
templates are supported. Hidden caches/lifecycle state and uploaded skills are
excluded; static skill bundle/catalog metadata is included. The file manifest
is the exact inventory: arbitrary binaries or unrecognized extensions are not
a guarantee of a complete third-party skill distribution.

Runtime credentials, `.env` (including examples), sessions, logs, recordings,
voice references, model weights, caches, checkpoints, backups, runtime config,
vendor environments and `.tooling` do not ship. Optional IndexTTS vendor source
and its locked environment are also excluded. `bootstrap_voice.ps1` will refuse
to proceed without the separately supplied IndexTTS tree; it cannot provision
voice from this source bundle alone. See `docs/VOICE_RUNTIME.md` for workstation
requirements. Live capability results on the development machine do not prove
those capabilities on a fresh computer.

Third-party LICENSE/NOTICE/COPYING files within selected sources are retained;
skills with the known transfer-prohibition text and reviewed upstream secret
examples are excluded. No project LICENSE/COPYING currently exists; the root
README assertion is unverified. Packaging creates no licensing grant, and the
manifest always reports `redistribution_ready: false`. Any future redistribution
needs separate attribution/license review and a project-owner license decision.

The targeted literal scan fails without printing suspected credentials. It is
not proof of absence of every secret; the release owner must review the actual
inventory and docs before public distribution. No dependency, native host or
external tool installation is performed by packaging or verification.
