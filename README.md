# WISE Agent

![Python](https://img.shields.io/badge/Python-3.13%20tested-3776AB?logo=python&logoColor=white)
![Desktop](https://img.shields.io/badge/Desktop-Windows-555555)
![Status](https://img.shields.io/badge/Status-active%20development-555555)

A local-first agent workspace for configured language models, project files,
research, skills, MCP tools and guarded desktop operations. WISE combines a
FastAPI backend with a native Windows host and a lightweight web-rendered UI.
The agent uses the provider/model selected by the user; a configured account
does not imply permission to perform every action.

## What is implemented

- Persistent conversations, project attachments and actual workspace edits.
- Canonical capability routing and bounded, on-demand skill discovery.
- A Usage Network connecting research, inspection, modification, security and
  verification phases, with observed calls distinguished from recommendations.
- Public research sources, selected typed API adapters and connected MCP tools.
- Reviewed local defensive scanners installed into isolated environments when
  needed, with integrity checks, leases and removable idle caches.
- Approval boundaries for sensitive operations and official Windows UAC flows;
  no UAC bypass is claimed.
- Playwright/FlaUI acceptance harnesses with screenshots, logs and reproduction
  steps, plus regression and independent-result checks.

## Run locally

```powershell
git clone https://github.com/qwzx4893-stack/WISE-Agent.git
cd WISE-Agent
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn api.server:app --host 127.0.0.1 --port 8765
```

The app is available at `http://127.0.0.1:8765/app/`. For the native Windows
host, use the supplied `Start-WISE*.ps1` launcher after checking its documented
prerequisites. Configure your own model provider in settings. Credentials,
conversations, local media, voice weights, training data and test runtime
folders are deliberately not included in this source snapshot.

## Security and readiness

This is an active-development project, not a certified zero-bug product or a
claim of parity with commercial agents. Tool availability depends on the
environment. A public-page reader is not the full API/product; many catalog
entries still require an adapter. On Windows, bounded workers and command
allowlists are not equivalent to a complete OS sandbox.

Bind the API to loopback. For non-local deployments, configure
`AGENT_API_TOKEN` and an appropriate transport/network boundary. Never commit
provider keys, `.env`, account connections or runtime data. Review artifacts
and source attribution before relying on security-sensitive research.

## Documentation

- [On-demand security tools](docs/OPTIONAL_SECURITY_TOOLS.md)
- [Evidence-based validation](docs/WISE_FINAL_VALIDATION_PLAN.md)
- [Developer docs](docs/)

## Snapshot scope and licensing

This snapshot precedes the new Nango Plugins work. That subsequent layer is
kept local until the owner explicitly approves publishing it. No successful
GitHub CI badge or complete-production readiness claim is fabricated here.

Third-party materials retain their supplied license/notice files and upstream
attributions. This repository does not assert a blanket license for every
imported skill, model or dependency; review their respective terms before
redistribution or deployment. Voice/model weights are not redistributed.
Imported skills with explicit redistribution prohibitions are omitted from
this public snapshot; their local copies are not removed.
