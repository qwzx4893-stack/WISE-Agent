"""Export every document resource's actual adapter contract, not marketing claims."""
import json
import argparse
import sys
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from core.intelligence.inventory import inventory,QUERY_IDS,ALIASES
from source_stamp import source_stamp

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows=inventory()
    counts=dict(Counter(row["status"] for row in rows))
    destination=args.output.resolve()
    destination.mkdir(parents=True,exist_ok=False)
    report={"generated_at":datetime.now(timezone.utc).isoformat(),"source_stamp":source_stamp(),
        "counts":counts,"canonical_entries":len(rows),"aliases":ALIASES,
        "scope":"Adapter/dependency availability, not full upstream capability or endpoint health",
        "user_priority":"Complete standalone tools first; defer large service/platform deployments",
        "entries":rows}
    (destination/"resource-coverage.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# WISE — تغطية أدوات ومصادر الملف","",
        "هذه قائمة تنفيذ فعلية وليست إعلاناً بأن كل الموارد مثبتة أو بأن كل قدرات منتجاتها متاحة. توافر المحوّل لا يثبت صحة الخدمة الآن أو إكمال المصادقة أو الترخيص.","",
        f"المدخلات الموحدة: {len(rows)}. حالات المحوّلات: `{counts}`.",
        "GDELT Project اسم بديل لـGDELT وليس أداة ثانية. أُضيف Headscale المذكور في الملف، وBandit الموجود في المشروع.","",
        "قرار المستخدم: الأدوات المستقلة أولاً؛ نشر المنصات الكبيرة مؤجّل. الأدوات التي حالتها ADAPTER_REQUIRED أو DEPENDENCY_REQUIRED لم يكتمل دمجها بعد، ولا تُحسب قدرات جاهزة.","",
        "المصادر ذات query توفر استعلاماً محدداً؛ المصادر ذات read فقط توفر قراءة صفحة عامة، لا API كاملاً. أدوات inspect محدودة بالعقود أدناه، وليست كل وظائف البرنامج الأصلي.",""]
    for kind in ("tool","source","platform","model"):
        lines += ["## "+kind,"","| المورد | التصنيف | الحالة | الإجراءات | الحد الفعلي / المطلوب |","| --- | --- | --- | --- | --- |"]
        for row in sorted((row for row in rows if row["kind"]==kind),key=lambda row:(row["category"],row["name"])):
            safe=lambda value:str(value).replace("|","/").replace("\n"," ")
            name=f"[{safe(row['name'])}]({row['url']})" if row.get("url") else safe(row["name"])
            lines.append("| "+" | ".join((name,safe(row["category"]),row["status"],", ".join(row["actions"]) or "—",safe(row["limitations"])))+" |")
        lines.append("")
    (destination/"RESOURCE_COVERAGE.md").write_text("\n".join(lines),encoding="utf-8")
    print(json.dumps({"entries":len(rows),"counts":counts,"report":str(destination/"RESOURCE_COVERAGE.md")}))
if __name__=="__main__":main()
