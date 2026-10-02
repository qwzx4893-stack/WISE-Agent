import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modes.workflow import Workflow

wf = Workflow()

print("🧠 Agent OS v24 - BALANCED SAFE CORE")
print("Commands: <task> | exit")

while True:
    t = input("\n> ").strip()
    if t.lower() == "exit":
        break
    if not t:
        continue
    print("\n" + wf.run(t))
