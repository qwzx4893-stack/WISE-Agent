"""Compatibility entry point for the package-store journeys.

The former MCP-directory UI is retired. Historical MCP directory reports are
not acceptance evidence for the package shop. Protocol coverage remains in
test_plugins.py and plugin_live_probe.py.
"""
from package_store_journeys import main

if __name__ == "__main__":
    raise SystemExit(main())
