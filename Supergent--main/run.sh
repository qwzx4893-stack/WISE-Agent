#!/bin/bash
cd ~/agent-os
source ~/agent-os-env/bin/activate 2>/dev/null || true
export PYTHONPATH=~/agent-os
python3 agent_core.py "$@"
