"""Reviewed service connections, not executable marketplace packages.

Endpoints come from vendor documentation. Listing does not assert that a
vendor accepts WISE's public OAuth client or that the owner has a paid plan.
No downloads, password scraping, borrowed client IDs, or automatic tool calls.
"""
from pathlib import Path
from .catalog import featured, field

# id, name, endpoint, vendor documentation, category, Simple Icons identity,
# description. Keep sources beside recipes so endpoint changes are reviewable.
SERVICES = [
    ('canva','Canva','https://mcp.canva.com/mcp','https://www.canva.dev/solutions/mcp/','Design','canva','Create, edit and export designs with your Canva account.'),
    ('miro','Miro','https://mcp.miro.com','https://developers.miro.com/docs/miro-mcp-server-frequently-asked-questions','Design','miro','Read and work with your boards and diagrams.'),
    ('webflow','Webflow','https://mcp.webflow.com/mcp','https://developers.webflow.com/mcp/reference/getting-started','Design','webflow','Work with your sites and CMS collections.'),
    ('dropbox','Dropbox','https://mcp.dropbox.com/mcp','https://help.dropbox.com/integrations/connect-dropbox-mcp-server','Productivity','dropbox','Find and work with files in your Dropbox account.'),
    ('todoist','Todoist','https://ai.todoist.net/mcp','https://developer.todoist.com/','Productivity','todoist','Organize projects and tasks in your Todoist account.'),
    ('monday','monday.com','https://mcp.monday.com/mcp','https://developer.monday.com/api-reference/docs/build-on-monday-with-ai','Productivity','mondaydotcom','Read and update boards, items and workflows.'),
    ('clickup','ClickUp','https://mcp.clickup.com/mcp','https://clickup.com/blog/how-to-use-clickup-mcp-server/','Productivity','clickup','Work with tasks and projects in your workspace.'),
    ('airtable','Airtable','https://mcp.airtable.com/mcp','https://support.airtable.com/articles/9897799762-using-the-airtable-mcp-server','Databases','airtable','Query and update the bases you can access.'),
    ('neon','Neon','https://mcp.neon.tech/mcp','https://neon.com/guides/neon-mcp-server-github-copilot-vs-code','Databases','neon','Manage Postgres projects and inspect databases.'),
    ('prisma','Prisma','https://mcp.prisma.io/mcp','https://www.prisma.io/docs/ai/tools/mcp-server','Databases','prisma','Work with Prisma Postgres projects and databases.'),
    ('gitlab','GitLab','https://gitlab.com/api/v4/mcp','https://docs.gitlab.com/user/model_context_protocol/mcp_server/','Development','gitlab','Access repositories, issues and development workflows.'),
    ('railway','Railway','https://mcp.railway.com','https://docs.railway.com/ai/mcp-server','Development','railway','Work with projects, environments and deployments.'),
    ('postman','Postman','https://mcp.postman.com/minimal','https://learning.postman.com/latest-v-12/docs/reference/postman-api/postman-mcp-server/postman-mcp-remote-server','Development','postman','Access API workspaces and collections using an API key.'),
    ('datadog','Datadog','https://mcp.datadoghq.com/v1/mcp','https://docs.datadoghq.com/mcp_server/setup/','Development','datadog','Inspect observability data on the US1 Datadog site.'),
    ('bugsnag','BugSnag','https://bugsnag.mcp.smartbear.com/mcp','https://developer.smartbear.com/smartbear-mcp/docs/remote-bugsnag','Development','bugsnag','Investigate errors and diagnostic data.'),
    ('intercom','Intercom','https://mcp.intercom.com/mcp','https://developers.intercom.com/docs/guides/mcp','Business','intercom','Read support conversations with your account permissions.'),
    ('hubspot','HubSpot','https://mcp.hubspot.com','https://developers.hubspot.com/ai-tools/mcp','Business','hubspot','Work with CRM objects and account workflows.'),
    ('amplitude','Amplitude','https://mcp.amplitude.com/mcp','https://amplitude.com/docs/amplitude-ai/amplitude-mcp','Data & AI','amplitude','Query analytics and work with dashboards on the US site.'),
    ('mixpanel','Mixpanel','https://mcp.mixpanel.com/mcp','https://docs.mixpanel.com/docs/features/mcp','Data & AI','mixpanel','Analyze events, funnels and retention on the US site.'),
    ('ahrefs','Ahrefs','https://api.ahrefs.com/mcp/mcp','https://docs.ahrefs.com/en/mcp/docs/introduction','Research','ahrefs','Research search performance and backlinks on your plan.'),
    ('exa','Exa','https://mcp.exa.ai/mcp','https://github.com/exa-labs/exa-mcp-server','Research','exa','Search the web and retrieve research context.'),
    ('tavily','Tavily','https://mcp.tavily.com/mcp/','https://docs.tavily.com/documentation/mcp','Research','tavily','Search the web and extract information.'),
    ('firecrawl','Firecrawl','https://mcp.firecrawl.dev/v2/mcp-oauth','https://github.com/firecrawl/firecrawl-docs/blob/main/mcp-server.mdx','Research','firecrawl','Search and extract web content with account sign-in.'),
    ('netlify','Netlify','https://netlify-mcp.netlify.app/mcp','https://docs.netlify.com/build/build-with-ai/agent-setup-guides/set-up-claude-code-for-netlify/','Development','netlify','Work with sites, builds and deployment resources.'),
    ('square','Square','https://mcp.squareup.com/mcp','https://developer.squareup.com/docs/mcp','Business','square','Access customers, orders and items in your Square account.'),
    ('attio','Attio','https://mcp.attio.com/mcp','https://mcp.attio.com/','Business','attio','Read and manage CRM records with your account permissions.'),
    ('ramp','Ramp','https://mcp.ramp.com/mcp','https://docs.ramp.com/developer-api/v1/guides/ramp-mcp-remote','Business','ramp','Review spend, transactions and business resources.'),
    ('klaviyo','Klaviyo','https://mcp.klaviyo.com/mcp','https://developers.klaviyo.com/en/docs/klaviyo_mcp_server','Business','klaviyo','Work with marketing data, segments and campaign insights.'),
    ('granola','Granola','https://mcp.granola.ai/mcp','https://help.granola.ai/article/granola-mcp','Productivity','granola','Find and use your meeting notes.'),
    ('wix','Wix','https://mcp.wix.com/mcp','https://www.wix.com/studio/developers/mcp-server','Design','wix','Build and manage sites and business solutions.'),
    ('zoom','Zoom','https://zoom.us/mcp/meeting/streamable','https://developers.zoom.us/docs/mcp/servers/','Productivity','zoom','Work with meeting resources using your Zoom permissions.'),
]

DESCRIPTIONS = {
    'notion':'Search and update pages in your Notion workspace.',
    'github':'Work with repositories, issues and pull requests.',
    'linear':'Read and organize issues, teams and projects.',
    'sentry':'Investigate application errors and performance.',
    'cloudflare-docs':'Search Cloudflare developer documentation. No account needed.',
    'context7':'Retrieve current documentation for programming libraries.',
    'figma':'Read design context from your Figma account.',
    'supabase':'Work with your Supabase projects and databases.',
    'vercel':'Inspect projects and deployments in your Vercel account.',
    'atlassian':'Work with Jira and Confluence using your account permissions.',
    'stripe':'Work with your Stripe account and payment resources.',
    'huggingface':'Find models, datasets and research resources.',
}

# These brands are absent from Simple Icons. Use artwork served by the
# service itself, not a similarly named icon or the repository owner's avatar.
VENDOR_ARTWORK = {
    'context7':'https://context7.com/favicon.ico',
    'canva':'https://www.canva.com/favicon.ico',
    'monday':'https://monday.com/favicon.ico',
    'bugsnag':'https://imagedelivery.net/PVooPtpJE-25QaNkbEuXvw/19ef04f9-e12a-4978-585b-3e57e97f3000/public',
    'amplitude':'https://amplitude.com/favicon.ico',
    'ahrefs':'https://ahrefs.com/favicon.ico',
    'exa':'https://exa.ai/favicon.ico',
    'tavily':'https://docs.tavily.com/mintlify-assets/_mintlify/favicons/tavilyai/SXaxSfweEU3ftIlh/_generated/favicon/android-chrome-192x192.png',
    'firecrawl':'https://www.firecrawl.dev/favicon.ico',
    'attio':'https://attio.com/favicon.ico',
    'ramp':'https://ramp.com/favicon.ico',
    'klaviyo':'https://www.klaviyo.com/icons/icon-32x32.png',
    'granola':'https://www.granola.ai/favicon/favicon-96x96.png',
}


def service_rows():
    rows = featured()
    assets = Path(__file__).resolve().parents[2]/'ui/wise_web/assets/integrations'
    for key,name,url,docs,category,brand,description in SERVICES:
        rows.append({'id':key,'publisher_id':key,'name':name,'description':description,
            'version':'service','source':'featured','categories':[category],
            'logo':f'/app/assets/integrations/{key}.svg' if (assets/(key+'.svg')).is_file() else f'https://cdn.simpleicons.org/{brand}',
            'fields':[],'docs_url':docs,'recipe':{'kind':'remote','url':url,'headers':[]},'unsupported':''})
    for row in rows:
        if row['id'] in VENDOR_ARTWORK: row['logo']=VENDOR_ARTWORK[row['id']]
        elif row['id']=='prisma': row['logo']='https://cdn.simpleicons.org/prisma/ffffff'
        row['description'] = DESCRIPTIONS.get(row['id'],row['description'])
        row.update(display_name=row['name'], publisher=row['name'], connection_type='cloud',
            category=row['categories'][0], auth_mode='oauth', download_required=False)
        if row['id'] in {'cloudflare-docs','exa'}: row['auth_mode']='public'
        if row['id']=='postman':
            header={'name':'Authorization','description':'Postman API key','isRequired':True,'isSecret':True,'format':'bearer'}
            row['fields']=[field(header,'header',0)];row['recipe']['headers']=[header];row['auth_mode']='token'
        # Vendor registration is not bypassed with another client's identity.
        if row['id'] in {'figma','canva','hubspot'}:
            row['setup_note']='The service may require approval or registration of WISE as an OAuth client. See its setup guide if sign-in is refused.'
    rows.append({'id':'figma-desktop','publisher_id':'figma','name':'Figma Desktop','display_name':'Figma Desktop',
        'publisher':'Figma','description':'Use design context from Figma already running on this computer.',
        'version':'service','source':'featured','categories':['Local apps'],'category':'Local apps',
        'logo':'https://cdn.simpleicons.org/figma','fields':[],
        'docs_url':'https://developers.figma.com/docs/figma-mcp-server/local-server-installation/',
        'recipe':{'kind':'local-app','url':'http://127.0.0.1:3845/mcp','headers':[]},
        'unsupported':'','connection_type':'local','auth_mode':'local','download_required':False,
        'setup_note':'Open Figma Desktop, enter Dev Mode and enable its desktop MCP server. WISE does not install or start Figma.'})
    from .local import local_rows
    rows.extend(local_rows())
    return rows


def catalog(manager, query='', category='', offset=0, limit=48):
    # Background refresh fetches metadata only. No install, login or tool call.
    manager.refresh()
    if query: manager.search_directory(query)
    from .market import describe
    with manager.lock:
        ids={row['id'] for row in service_rows()} | {row['id'] for row in manager.cache.get('items',[]) if row.get('source') in {'bundle','community'}}
        ids.update(key for key,record in manager.installed.items() if record['entry'].get('source') in {'bundle','community'})
        rows=[manager._present(describe(row) if row.get('source')=='community' else row)
              for row in manager._rows(include_internal=False) if row['id'] in ids]
    rows.sort(key=lambda row:(row['status']!='CONNECTED',row.get('source')!='featured',not row.get('can_connect'),row['name'].casefold(),row['id']))
    categories=list(dict.fromkeys(row['category'] for row in rows))
    visible=[row for row in rows if (not category or category=='All MCPs' or row['category']==category)
        and (not query or query.casefold() in (row['name']+' '+row['description']+' '+row['category']+' '+row.get('publisher_id','')).casefold())]
    sections=[{'name':name,'items':[row for row in rows if row['category']==name][:6]} for name in categories]
    sections.append({'name':'All MCPs','items':rows[:6]})
    return {'items':visible[offset:offset+limit],'total':len(visible),'next_offset':offset+limit if offset+limit<len(visible) else None,
        'categories':categories,'sections':sections,'syncing':manager.syncing,'warning':manager.warning,'kind':'mcp-market'}
