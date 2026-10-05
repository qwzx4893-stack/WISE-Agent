from core.capability_network import CapabilityNetwork, tokenize
from core.capability_router import CapabilityDescriptor
from core.contracts import CapabilitySource


def _cap(identifier, name, source, description, risk="LOW"):
    return CapabilityDescriptor(
        id=identifier, name=name, source=source, description=description,
        risk_level=risk, availability=True,
    )


def test_arabic_research_prefers_rag_and_returns_relevant_skill():
    network = CapabilityNetwork()
    plan = network.plan(
        "ابحث عن أحدث المراجع وقارن المصادر حول أمان الوكلاء",
        capabilities=[
            _cap("rag.deep", "deep_research", CapabilitySource.RAG, "Multi-source web research and citations."),
            _cap("computer.action", "computer_action", CapabilitySource.COMPUTER_USE, "Click and type on desktop."),
        ],
        skills={
            "research-audit": {"description": "Research sources and compare citations", "category": "knowledge", "path": "/skills/research"},
            "desktop-click": {"description": "Click desktop buttons", "category": "browser"},
        },
    )

    assert "ابحث" in tokenize("ابحث عن مصادر")
    assert plan.capabilities[0].id == "rag.deep"
    assert plan.skills[0].name == "research-audit"
    assert "شبكة اختيار القدرات" in plan.to_prompt()


def test_mcp_signal_prefers_connected_mcp_without_hiding_lower_risk_context():
    network = CapabilityNetwork()
    plan = network.plan(
        "استعلم من قاعدة البيانات المتصلة عبر MCP عن حالة المشروع",
        capabilities=[
            _cap("native.file", "read_file", CapabilitySource.NATIVE, "Read a local workspace file."),
            _cap("mcp.db.query", "query_database", CapabilitySource.MCP, "Query the connected project database."),
        ],
        skills={},
    )

    assert plan.capabilities[0].source == "MCP"
    assert plan.capabilities[0].name == "query_database"


def test_unavailable_capabilities_are_never_recommended():
    cap = _cap("danger", "delete_everything", CapabilitySource.NATIVE, "Delete files.", "CRITICAL")
    cap.availability = False
    plan = CapabilityNetwork().plan("احذف ملف", capabilities=[cap], skills={})
    assert plan.capabilities == []


def test_no_match_does_not_return_an_arbitrary_available_tool():
    plan = CapabilityNetwork().plan(
        "سؤال تجريدي لا يطابق أي قدرة مسجلة",
        capabilities=[_cap("native.random", "random_tool", CapabilitySource.NATIVE, "Unrelated operation.")],
        skills={},
    )
    assert plan.capabilities == []
    assert "search_tools" in plan.to_prompt()
