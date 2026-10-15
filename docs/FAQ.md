# Troubleshooting and FAQ

[README](../README.md) · [Installation](INSTALLATION.md) · [Support](../.github/SUPPORT.md)

## What is WISE?

A Windows-first Agent OS workspace connecting cognition, capability discovery, multi-agent execution, tools, models, durable state and desktop automation. It is a development preview, not merely a model chat interface and not yet a certified unattended assistant.

## Why does installation reject my Python version?

The documented installer requires **Python 3.11**. Check `python --version`; if PATH selects another interpreter, pass the Python 3.11 executable using the installer's `-Python` argument. Do not install project dependencies into an unrelated global environment to work around it.

## How do I launch the actual desktop window?

After installing from the repository root:

```powershell
.\Supergent--main\.venv\Scripts\python.exe .\Supergent--main\wise_desktop.py --native-only
```

Use the explicit project interpreter. A backend URL alone is not the native application. WebView2 is required for rendering; its name does not mean WISE opens in the Edge browser.

## Is a model, account or custom voice included?

No. Configure your own provider/local model. Optional voice requires separately provisioned compatible upstream code, weights and permitted assets. A missing voice asset must be treated as unavailable, not silently replaced with a fictitious installed voice.

## Why does an MCP entry need configuration instead of connecting immediately?

A catalog describes servers; it does not grant account access or install every local application. Browser OAuth is possible only when the server supports it. Other servers require a service credential or trusted local command. Review the [connection guide](../Supergent--main/docs/PLUGINS.md) and [MCP Market documentation](../Supergent--main/docs/MCP_CONNECTION_MARKETPLACE.md). The current direct path does not require Nango.

## Does local-first mean nothing leaves my device?

No. Cloud model prompts, searches, remote MCP calls and messages can transmit data. Use only intended services and review outgoing scope. Complete destination/data/scope enforcement remains an open acceptance gate.

## Are all tools and skills ready after the base install?

No. Discovery, installation, authentication, authorization and verified execution are distinct states. Voice, semantic discovery, advanced compression, external tools and Leon services have separate prerequisites. See the [dependency matrix](INSTALLATION.md#dependency-matrix).

## Can I use it on Linux because Linux CI passes?

Linux CI tests source behavior. It does not certify a native Linux desktop release. Windows 10/11 is the primary documented desktop target.

## Do passing badges mean daily use is guaranteed safe?

No. They show workflow results. Native Windows, real accounts, research correctness, dependency remediation, voice hardware and long-duration use have separate gates. See [Validation](VALIDATION.md).

## May I reuse everything because the repository is public?

Not automatically. WISE-specific source has no repository-wide license grant yet; third-party components retain their own terms. Read [notices](../THIRD_PARTY_NOTICES.md) and [licensing status](../Supergent--main/docs/releases/LICENSING_STATUS.md).

## What should I include in a report?

Commit, environment, exact launch command, expected/actual behavior, minimal synthetic reproduction and redacted evidence. Do not include tokens, browser state or personal content. Security issues follow [private disclosure guidance](../SECURITY.md).
