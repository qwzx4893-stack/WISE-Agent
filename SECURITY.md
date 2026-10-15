# Security policy

WISE is a development-preview Agent OS that can edit files, execute commands, control browsers and connect to accounts. It is not a certified production release or a guarantee of safe unattended operation.

## Supported security status

| Channel | Status |
| --- | --- |
| `main` | Active hardening; no stable support guarantee. |
| Historical snapshots / forks | No promised backport or independent security support. |
| Optional runtimes / services | Separate upstream dependencies, policies and acceptance requirements. |

Passing source tests do not establish that the entire system is vulnerability-free. [Validation](docs/VALIDATION.md) records remaining dependency, egress, native Windows and real-account acceptance limits.

## Report a vulnerability privately

1. Use **GitHub private vulnerability reporting**, if enabled in the repository's [Security area](https://github.com/qwzx4893-stack/WISE-Agent/security).
2. If unavailable, use an existing private contact channel with [the repository owner](https://github.com/qwzx4893-stack). No dedicated security email or response-time commitment has been established.
3. If you have no private channel, open only a minimal request for a private contact route. **Do not include the vulnerability, exploit or private data in that public request.**

Include the affected commit/component, expected and actual behavior, prerequisites, impact and a minimal synthetic reproduction. Explain whether the issue crosses authentication, approval, filesystem, native bridge, MCP/OAuth or data-export boundaries.

Never include real tokens, cookies, browser storage, personal documents or recordings. Do not test accounts, machines or services without explicit authorization. Do not publish exploit details before coordinated handling.

## Operational precautions

- Use a dedicated workspace and low-risk data while evaluating the preview.
- Keep the API loopback-only unless authentication and trusted origins are deliberately configured.
- Review high-impact actions. Model instructions and claims of user benefit are not access controls.
- Use official UAC consent for administrative operations. Bypass is unsupported.
- Treat local Python, commands and stdio MCP as trusted user-level execution, **not an OS sandbox**.
- Keep credentials and profiles separate; never commit secrets or runtime state.
- Review permissions, recipients and outgoing data before connecting external services.

If a credential is exposed, revoke or rotate it with the issuer. Deleting a file or commit does not revoke a credential. Do not paste replacement credentials into issues or pull requests.

For non-sensitive bugs and setup questions, use the [support guide](.github/SUPPORT.md).
