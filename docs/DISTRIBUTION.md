# Public source distribution contract

This publishes current workspace **source**, not a Windows installer or a copy of the owner's machine. The older pre-Plugins snapshot is superseded with Git history preserved.

Included: Agent OS, current WISE UI/assets, configuration examples, grouped skill/tool metadata and permitted source, tests/QA harness source, runtime/optional dependency manifests, supervisors, Leon source/licenses/locks, setup and documentation.

Excluded: credentials, conversations/memory/tasks/account state, cookies/profiles, private logs/QA outputs, installed environments, executables/models/recordings/training data, comparison UIs/backups, known restricted skills and reviewed credential-like upstream fixtures. Generated executable shims are not portable install recipes.

Voice needs upstream source/weights and separately supplied permitted custom assets. Leon/services need documented setup. Catalog entries are not installed executables, deployed platforms or authenticated accounts.

`PUBLICATION_MANIFEST.json` records public payload hashes, required-file checks, exclusions, reviewed credential-example redactions and scope. Redactions affect only pinned documentation examples in the exported copy; local source and all runtime code remain unchanged. This does not rotate historical exposures. It is not full license clearance, a signed release or clean-machine acceptance. Targeted secret scanning cannot prove arbitrary bytes contain no sensitive information.

The development checkout and existing uncommitted changes are not staged/reset. Publication builds a separate reviewed payload, compares it with the existing remote `main`, and creates a normal descendant commit with an expected-head check and no force push. It may use an isolated clone or the authenticated GitHub connector; no plaintext credential is required in repository files or commands.
