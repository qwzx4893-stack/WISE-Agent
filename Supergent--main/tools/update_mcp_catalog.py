"""Refresh the distributable metadata-only MCP Registry snapshot. No installation."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.plugins.catalog import bounded_json
from core.plugins.market import SEED,fetch_snapshot

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--community',action='store_true');parser.add_argument('--popular',action='store_true');args=parser.parse_args()
    if args.popular:
        snapshot=json.loads(SEED.read_text(encoding='utf-8'))
        rows={raw['server']['name']:raw for raw in snapshot['servers']}
        for query in ('microsoft','awslabs','modelcontextprotocol','mongodb','qdrant','jina','brave','sequential','google','elastic','obsidian','perplexity','playwright','upstash'):
            data=bounded_json('https://registry.modelcontextprotocol.io/v0.1/servers',params={'version':'latest','limit':30,'search':query})
            from core.plugins.market import registry_row
            for raw in data.get('servers',[])[:30]:
                if registry_row(raw): rows[raw['server']['name']]=raw
            print(query,len(data.get('servers',[])),flush=True)
        snapshot.update(time=__import__('time').time(),servers=list(rows.values())[:8000],complete=False,scope='Bounded general/GitHub snapshot plus selected ecosystem searches; online search supplements it')
    else:
        snapshot=fetch_snapshot(bounded_json,search='io.github' if args.community else '',maximum=1000 if args.community else 6000)
    if not snapshot['servers']: raise SystemExit('No active listings returned; existing snapshot left intact')
    if args.community and SEED.is_file():
        previous=json.loads(SEED.read_text(encoding='utf-8'))
        rows={raw['server']['name']:raw for raw in previous['servers']}
        rows.update({raw['server']['name']:raw for raw in snapshot['servers']})
        snapshot.update(servers=list(rows.values()),complete=False,scope='General catalog plus GitHub community namespace; bounded snapshot, online search supplements it')
    SEED.parent.mkdir(parents=True,exist_ok=True)
    staging=SEED.with_suffix('.json.tmp')
    staging.write_text(json.dumps(snapshot,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    staging.replace(SEED)
    print(json.dumps({'entries':len(snapshot['servers']),'complete':snapshot['complete'],'next_cursor':snapshot['next_cursor'],'path':str(SEED)}),flush=True)
