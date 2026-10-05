"""Only verified connected accounts expose reviewed tools in the canonical router."""
import os


def refresh_integrations(router):
    prefix = "extension.plugins."
    for identifier in list(router._capabilities):
        if identifier.startswith(prefix): router._capabilities.pop(identifier)
    if not os.getenv("NANGO_SECRET_KEY"): return
    from .service import get_integration_service, READ_TOOLS
    from core.capability_router import CapabilityDescriptor
    from core.contracts import CapabilitySource
    service = get_integration_service()
    try:
        accounts = [row for row in service.accounts() if row["provider"] == "github" and row["status"] == "CONNECTED"]
    except Exception:
        # No false connected tools on provider failure; errors shown on Plugins page.
        return
    if not accounts: return
    for operation, description in READ_TOOLS.items():
        properties = {"account": {"type": "string", "enum": [row["id"] for row in accounts], "description": "Choose one connected account"}}
        required = ["account"]
        if operation == "search_repositories":
            properties["query"] = {"type": "string", "maxLength": 300}; required.append("query")
        if operation == "read_file":
            for name in ("owner", "repo", "path"): properties[name] = {"type": "string", "maxLength": 400}; required.append(name)
        router.register_extension(CapabilityDescriptor(id=prefix + operation, name=operation, source=CapabilitySource.TOOL_PACK,
            description=description + ". Connected accounts: " + ", ".join(f'{row["label"]} ({row["id"]})' for row in accounts),
            input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            risk_level="LOW", runtime_status="CONNECTED", category="integrations",
            execution_adapter=lambda args, operation=operation: service.execute_read(operation, args)), trusted=True)
