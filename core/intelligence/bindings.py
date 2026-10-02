"""Bind inventory entries to the canonical router with explicit readiness."""
from .inventory import inventory

def register_intelligence(router):
    from core.capability_router import CapabilityDescriptor
    from core.contracts import CapabilitySource
    from core.security import get_security_gate, ActionTier
    from .sources import execute_source
    from .tools import execute_tool
    for resource in inventory():
        identifier=resource["id"]
        is_source=resource["kind"] == "source"
        key=("source." if is_source else "intelligence.")+identifier
        schema=({"action":{"type":"string","enum":resource.get("actions",[])},
            "query":{"type":"string","maxLength":500},"limit":{"type":"integer","minimum":1,"maximum":50},
            "country":{"type":"string"},"indicator":{"type":"string"}} if is_source else
            {"path":{"type":"string"},"action":{"type":"string","enum":resource.get("actions",[])},
             "rules_path":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100}})
        adapter=(lambda params, rid=identifier:execute_source(rid,**params)) if is_source else (lambda params,rid=identifier:execute_tool(rid,**params))
        # All executable adapters here are public reading or bounded offline
        # inspections. Unimplemented services/active scanners have no adapter.
        get_security_gate().register_action_tier(key,ActionTier.READ)
        router.register(CapabilityDescriptor(id=key,name=key,source=CapabilitySource.NATIVE,
            description=resource["name"]+": "+resource["description"]+". "+resource.get("limitations", ""),
            input_schema=schema,category=resource["category"],availability=resource["available"],
            runtime_status=resource["status"],missing_reason=None if resource["available"] else resource["required_next_step"],
            execution_adapter=adapter if resource["available"] else None))
