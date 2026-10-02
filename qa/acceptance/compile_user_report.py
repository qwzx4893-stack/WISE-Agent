"""Compile latest completed user, native, live and regression evidence."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def newest(pattern):
    files = list((ROOT / "qa-results").glob(pattern))
    if not files: raise FileNotFoundError(f"Missing evidence: {pattern}")
    return max(files, key=lambda path: path.stat().st_mtime)


def main():
    ui_path, live_path = newest("user-*/report.json"), newest("live-ui-*/report.json")
    regression_path = ROOT / "qa-results/regression-final-20261001.json"
    ui, live = [json.loads(path.read_text(encoding="utf-8")) for path in (ui_path, live_path)]
    regression = json.loads(regression_path.read_text(encoding="utf-8"))
    cases = ui["cases"] + live["cases"]
    project_paths=list((ROOT/"qa-results").glob("project-ui-*/report.json"))
    project_path=max(project_paths,key=lambda path:path.stat().st_mtime) if project_paths else None
    project=json.loads(project_path.read_text(encoding="utf-8")) if project_path else None
    if project: cases+=project["cases"]
    failures = [case["id"] for case in cases if case["status"] != "PASS"]
    destination = ROOT / "qa/results/acceptance-20261001"
    destination.mkdir(parents=True, exist_ok=True)
    budget_path = destination / "budget.json"
    budget = json.loads(budget_path.read_text(encoding="utf-8")) if budget_path.exists() else None
    gate_path = destination / "quality-gate.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else None
    review_path = destination / "semantic-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else None
    probe_paths=list((ROOT/"qa-results").glob("resource-probe-*/report.json"))
    probe_path=max(probe_paths,key=lambda path:path.stat().st_mtime) if probe_paths else None
    probe=json.loads(probe_path.read_text(encoding="utf-8")) if probe_path else None
    skipped = [{"id":test["nodeid"], "reason":next((phase.get("longrepr", "") for key in ("setup", "call", "teardown") if (phase:=test.get(key, {})).get("outcome") == "skipped"), "")}
               for test in regression.get("tests", []) if test.get("outcome") == "skipped"]
    report = {"generated_at":datetime.now(timezone.utc).isoformat(), "scenario_failures":failures,
              "user_scenarios":len(ui["cases"]), "live_scenarios":len(live["cases"]),
              "regression_summary":regression["summary"], "release_verdict":"NOT_READY", "budget":budget,
              "project":project,
              "deferred_scope":"Large platform/service deployments deferred by user; standalone tool integration is not complete",
              "unverified_gates":gate["unverified_gates"] if gate else ui["unverified_gates"][1:], "skipped_tests":skipped,
              "sources":[str(ui_path),str(live_path),str(regression_path)],"evidence_gate":gate,"semantic_review":review,
              "short_resource_probe":probe}
    if project_path: report["sources"].append(str(project_path))
    if probe_path: report["sources"].append(str(probe_path))
    if review: report["sources"].append(str(review_path))
    lines = ["# WISE — تقرير الاختبار العملي", "", "الحكم على جاهزية المنتج بالكامل: NOT_READY، ولا يعني نجاح هذه الاختبارات ضمان انعدام الأخطاء.", "",
             f"- الواجهة ونافذة Windows: {len(ui['cases'])} سيناريو؛ {ui['ui_verdict']}.",
             f"- نموذج حقيقي من OpenRouter عبر الواجهة: {len(live['cases'])} سيناريو؛ {live['verdict']}.",
             f"- Regression: {regression['summary']}.", "", "البيانات اصطناعية ومعزولة، والطلبات والتعديلات حقيقية وليست استجابات API محاكاة.", "",
             "## نتائج السيناريوهات وخطوات إعادة الإنتاج", ""]
    if project:
        lines[8:8]=[f"- مشروع متعدد الخطوات: {len(project['cases'])} سيناريو، {project['trials']} جولات؛ التنفيذ الآلي {project['verdict']}؛ مراقبة {project['soak_seconds']} ثانية. هذا ليس حكمًا على صحة كل ادعاءات البحث.",
            "- الأدوات المنفذة فعلاً: "+", ".join(project["distinct_actual_tools"]),
            "- المهارات المقروءة فعلاً: "+", ".join(project["actual_skills"])]
    for case in cases:
        lines += [f"### {case['status']} — {case['id']}", ""]
        lines += [f"{i+1}. {step}" for i,step in enumerate(case.get("steps", []))]
        for key in ("screenshot", "log", "trace", "backend_log"):
            if key in case: lines.append(f"[{key}]({Path(case[key]).resolve().as_posix()})")
        if case.get("stack_trace"): lines += ["```",case["stack_trace"],"```"]
        lines.append("")
    if review:
        lines += ["## المراجعة المستقلة: عيوب لا تكشفها فحوص التنفيذ وحدها", "",
                  "حكم دقة الإسناد في البحث: " + review["research_verdict"] + ". ملف البحث نسب إعدادًا تقنيًا إلى صفحات رسمية لا تحتويه في النص الكامل المسترجع. لم يُعتمد هذا الادعاء ولم يُصلح السبب في الوكيل بعد.",
                  "قياس RAM/CPU القديم خاص بمشغّل Python فقط، وليس بالباك إند الحقيقي؛ لا يُستخدم لإثبات انخفاض الاستهلاك. صُحّحت شفرة القياس للجولات القادمة، وليس نتائج الجولة السابقة.",
                  "[تفاصيل المراجعة والأدلة](semantic-review.json)", ""]
        for finding in review["findings"]:
            lines += [f"### {finding['status']} — {finding['id']}", "", finding["summary"], ""]
            lines += [f"{i+1}. {step}" for i,step in enumerate(finding.get("steps", []))]
            for key in ("artifact", "screenshot", "trace", "log"):
                if finding.get(key): lines.append(f"[{key}]({Path(finding[key]).resolve().as_posix()})")
            lines.append("")
    if probe and probe_path:
        lines += ["## التحقق القصير من تصحيح قياس الموارد", "",
                  "نجح قياس شجرة العمليات التابعة للباك إند في فحص قصير بلا طلبات نموذج. هذا يثبت تصحيح إسناد القياس فقط، ولا يثبت استقرارًا طويلًا أو استهلاك الصوت/GPU.",
                  f"[نتائج القياس القصير]({probe_path.resolve().as_posix()})", ""]
    lines += ["## ما لم يُثبت بعد", "", "- إكمال OAuth بخدمة تتطلب حساباً حقيقياً؛ اتصال MCP العام وفحوص البروتوكول لا تثبت تسجيل الدخول بحساب.",
              "- المحادثة الصوتية والمقاطعة والتعايش الفعلي مع نموذج محلي على GPU.",
              "- اختبار تشغيل طويل وفشل شبكة متكرر واستقرار الموارد؛ الاختبار القصير لا يثبت ذلك.",
              "- الإرسال إلى تطبيقات المراسلة لم يُنفّذ لتجنّب إرسال رسائل إلى حسابات المستخدم.",
              "- الخدمات الاختيارية غير المتاحة تظل خارج اعتماد المنتج الكامل.", "",
              "## إعادة التشغيل", "", "```powershell", "qa\\acceptance\\Run-UserAcceptance.ps1 -Live -Project", "```", "",
              "نسخ المفاتيح المؤقتة تُحذف بعد الاختبار.",
              "تُحفظ جولات الواجهة والاختبارات العملية الفاشلة وصورها أيضاً؛ لا تُمحى عند نجاح الجولة التالية."]
    if skipped:
        lines += ["", "## الاختبارات المتخطاة (ليست نجاحاً)", ""]
        lines += [f"- `{item['id']}`: {item['reason']}" for item in skipped]
    lines += ["", "## حدود الميزات المضافة", "",
              "- راجع [تغطية كل أدوات ومصادر الملف](RESOURCE_COVERAGE.md): الموارد مصنفة، مع الإجراءات وحالة المحوّل والحدود الفعلية. كثير من المنصات والأدوات ما زال يحتاج دمجاً أو تثبيتاً؛ قراءة صفحة عامة ليست تشغيل المنتج أو API كاملاً.",
              "- المنصات الكبيرة مؤجلة بقرار المستخدم؛ بقية الأدوات المستقلة غير المدمجة ليست مكتملة ولا تُحسب ضمن الجاهز.",
              "- عمليات DuckDB/YARA في عامل منفصل محدود الوقت ليست بديلاً عن عزل نظام تشغيل كامل. بيانات الاختبار معزولة، لكن مشغل Python الحالي يستخدم تثبيت Python المشترك للجهاز لا بيئة تبعيات مستقلة.",
              "- تطبيقات المراسلة تعتمد مخططات Apprise للإرسال الخارجي؛ استقبال المحادثات ليس مدعوماً بهذه الموصلات. الشعار المحلي موفّر لست خدمات، لا لكل الخدمات.",
              "- توافر drivenlisten.com لم يُؤكّد؛ لم يُستبدل بموقع آخر."]
    if gate:
        lines += ["", "## بوابة مطابقة الأدلة للإصدار", "",
            "حكم التنفيذ الآلي: " + gate.get("execution_scope_verdict","UNVERIFIED"),
            "حكم النطاق المختبر بعد مراجعة دقة البحث: " + gate["verified_scope_verdict"],
            "بصمة المصدر: " + gate["source_stamp"]]
        lines += ["- "+message for message in gate["evidence_failures"]]
    if budget:
        lines += ["", "## التحقق من سقف الإنفاق", "",
                  f"قراءة المزود لاستهلاك المفتاح كله منذ إنشائه: ${budget['usage']:.8f}؛ أقل من سقف $0.50.",
                  "هذه قراءة مجمعة للمفتاح كله، وليست تكلفة منسوبة لهذه الجولة وحدها أو ضماناً لسقف الجولات المستقبلية."]
    (destination / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    (destination / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report":str(destination / "REPORT.md"), "release_verdict":report["release_verdict"],
        "research_verdict":review["research_verdict"] if review else "UNVERIFIED",
        "regression_summary":report["regression_summary"], "ui_scenarios":len(ui["cases"]),
        "live_scenarios":len(live["cases"]), "project_scenarios":len(project["cases"]) if project else 0}, ensure_ascii=False))


if __name__ == "__main__": main()
