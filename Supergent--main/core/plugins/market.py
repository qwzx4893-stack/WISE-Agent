"""MCP Registry catalog metadata, never a publisher trust or execution grant."""
import copy
import json
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from .catalog import REGISTRY, from_registry, public_https

SEED = Path(__file__).resolve().parents[2]/'config/mcp_market_seed.json'
MAX_ITEMS = 8000


def registry_row(raw):
    row = from_registry(raw)
    if not row: return None
    spec = raw.get('server',raw)
    row['website_url'] = ''
    for key in ('websiteUrl',):
        try: row['website_url'] = public_https(spec.get(key,''))
        except ValueError: pass
    try: row['docs_url'] = public_https(row['docs_url'])
    except ValueError: row['docs_url'] = ''
    row['registry_url'] = 'https://registry.modelcontextprotocol.io/?q='+quote(row['publisher_id'],safe='')
    row['repository_url'] = ''
    try: row['repository_url'] = public_https(spec.get('repository',{}).get('url',''))
    except (ValueError,AttributeError): pass
    row['published_at'] = raw.get('_meta',{}).get('io.modelcontextprotocol.registry/official',{}).get('publishedAt','')
    row['updated_at'] = raw.get('_meta',{}).get('io.modelcontextprotocol.registry/official',{}).get('updatedAt','')
    result=describe(row)
    if not row.get('recipe'): result['connection_type']='manual'
    return result


def describe(row):
    row = copy.deepcopy(row)
    text = (row['name']+' '+row.get('description','')+' '+row.get('publisher_id','')).casefold()
    groups = [('Databases',('database','postgres','sqlite','mongodb','sql server','redis','snowflake')),
              ('Security',('security','vulnerability','pentest','cyber','threat','osint')),
              ('Design',('figma','design','canva','image','blender','photoshop')),
              ('Research',('search','research','crawl','web fetch','documentation','browser')),
              ('Development',('github','gitlab','code','developer','deployment','kubernetes','docker')),
              ('Business',('payment','crm','sales','commerce','finance','marketing')),
              ('Data & AI',('model','dataset','analytics','machine learning','llm')),
              ('Productivity',('calendar','email','notes','task','meeting','slack','notion'))]
    category = next((name for name,words in groups if any(word in text for word in words)),'Automation')
    recipe = row.get('recipe') or {}
    local = recipe.get('kind')=='package'
    row.update(display_name=row.get('display_name',row['name']),publisher=row.get('publisher_id',''),
               category=category,categories=[category],connection_type='manual' if not recipe else 'local-package' if local else 'cloud',
               download_required=local,auth_mode='local' if local else 'token' if any(f['required'] and f['secret'] for f in row.get('fields',[])) else 'oauth',
               origin_label='MCP Registry',registry_listing=True,
               setup_note='Community listing. Review the publisher, endpoint and permissions before connecting.' if not local else
               'Community package. Installation runs code with your account permissions. Review its publisher before granting access.')
    if not row.get('docs_url'): row['docs_url']=row.get('registry_url','')
    return row


def seed_rows():
    if not SEED.is_file() or SEED.stat().st_size>12_000_000: return []
    try:
        data=json.loads(SEED.read_text(encoding='utf-8'))
        return [row for raw in data.get('servers',[])[:MAX_ITEMS] if (row:=registry_row(raw))]
    except (OSError,ValueError,TypeError): return []


def fetch_snapshot(fetch, *, seconds=180, search='', maximum=MAX_ITEMS):
    """Bounded metadata-only scan; incomplete results never pretend to be full."""
    raw_rows={};cursor=None;seen=set();start=time.monotonic();complete=False
    for _ in range(80):
        params={'limit':100,'version':'latest'}
        if search: params['search']=search
        if cursor: params['cursor']=cursor
        data=fetch(REGISTRY,params=params)
        for raw in data.get('servers',[])[:100]:
            try:
                row=registry_row(raw)
                if row: raw_rows[row['publisher_id']]=raw
            except (ValueError,TypeError,AttributeError,KeyError): continue
        cursor=data.get('metadata',{}).get('nextCursor')
        if not cursor: complete=True;break
        if cursor in seen or len(raw_rows)>=maximum or time.monotonic()-start>seconds: break
        seen.add(cursor)
    return {'time':time.time(),'servers':list(raw_rows.values())[:maximum],
            'complete':complete,'source':REGISTRY,'next_cursor':cursor}
