import { Tool } from '@sdk/base-tool'

interface SupergentToolResult {
  tool: string
  success: boolean
  output: unknown
  error?: string
}

interface SupergentRAGResult {
  title: string
  snippet: string
  url: string
  source: string
}

interface SupergentWorkforceResult {
  task_id: string
  status: string
  subtasks: Array<{
    id: string
    description: string
    status: string
    result?: unknown
    error?: string
  }>
}

interface SupergentChannelResult {
  channel: string
  ok: boolean
  detail?: string
}

export default class SupergentBridgeTool extends Tool {
  private readonly baseUrl: string

  constructor() {
    super()
    this.baseUrl = (process.env['SUPERGENT_URL'] || 'http://127.0.0.1:8765').replace(/\/+$/, '')
  }

  private async request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    const url = `${this.baseUrl}${endpoint.startsWith('/') ? '' : '/'}${endpoint}`
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 30_000)

    try {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(options.headers as Record<string, string> || {})
      }

      const adminToken = process.env['AGENT_API_TOKEN']
      if (adminToken) {
        headers['X-Agent-Token'] = adminToken
      }

      const response = await fetch(url, {
        ...options,
        headers,
        signal: controller.signal
      })

      if (!response.ok) {
        const errorText = await response.text().catch(() => '')
        throw new Error(`Supergent API error [HTTP ${response.status}]: ${errorText || response.statusText}`)
      }

      return (await response.json()) as T
    } catch (error) {
      if ((error as Error).name === 'AbortError') {
        throw new Error(`Supergent request to "${endpoint}" timed out after 30s.`)
      }
      throw error
    } finally {
      clearTimeout(timeout)
    }
  }

  /**
   * Execute any tool from Supergent registry.
   */
  public async executeTool(params: {
    tool_name: string
    arguments?: Record<string, unknown>
  }): Promise<SupergentToolResult> {
    try {
      const toolName = params.tool_name.trim()
      const args = params.arguments || {}

      const result = await this.request<{ output?: unknown, error?: string }>(
        `/tools/execute/${encodeURIComponent(toolName)}`,
        {
          method: 'POST',
          body: JSON.stringify(args)
        }
      ).catch(async () => {
        // Fallback to generic chat ReAct loop execution
        return await this.request<{ answer: string }>(
          '/chat',
          {
            method: 'POST',
            body: JSON.stringify({
              message: `Execute tool: ${toolName} with arguments: ${JSON.stringify(args)}`,
              max_steps: 3,
              mode: 'normal'
            })
          }
        )
      })

      return {
        tool: toolName,
        success: true,
        output: result
      }
    } catch (error) {
      return {
        tool: params.tool_name,
        success: false,
        output: null,
        error: (error as Error).message
      }
    }
  }

  /**
   * Query Supergent 15 RAG sources.
   */
  public async searchRAG(params: {
    query: string
    sources?: string
    max_results?: number
  }): Promise<{ results: SupergentRAGResult[] }> {
    try {
      const query = params.query.trim()
      const sources = params.sources || 'wikipedia,arxiv,pubmed'
      const maxResults = params.max_results || 5

      const results = await this.request<SupergentRAGResult[]>(
        `/knowledge/search?query=${encodeURIComponent(query)}&sources=${encodeURIComponent(sources)}&max_results=${maxResults}`,
        { method: 'GET' }
      )

      return { results }
    } catch (error) {
      return {
        results: [
          {
            title: 'RAG Search Failure',
            snippet: (error as Error).message,
            url: '',
            source: 'error'
          }
        ]
      }
    }
  }

  /**
   * Run multi-agent workforce planning task.
   */
  public async runWorkforce(params: {
    task_description: string
  }): Promise<SupergentWorkforceResult> {
    try {
      const result = await this.request<SupergentWorkforceResult>(
        '/workforce/run',
        {
          method: 'POST',
          body: JSON.stringify({
            task: params.task_description
          })
        }
      )
      return result
    } catch (error) {
      return {
        task_id: 'error',
        status: 'failed',
        subtasks: [
          {
            id: 'subtask-0',
            description: params.task_description,
            status: 'failed',
            error: (error as Error).message
          }
        ]
      }
    }
  }

  /**
   * Send notification via Apprise (199 schemes).
   */
  public async sendNotification(params: {
    channel: string
    message: string
    title?: string
  }): Promise<SupergentChannelResult> {
    try {
      const result = await this.request<SupergentChannelResult>(
        '/channels/send',
        {
          method: 'POST',
          body: JSON.stringify({
            channel: params.channel,
            message: params.message,
            title: params.title || 'WISE Notification'
          })
        }
      )
      return result
    } catch (error) {
      return {
        channel: params.channel,
        ok: false,
        detail: (error as Error).message
      }
    }
  }

  /**
   * Search across Supergent 1,727 Agent Skills.
   */
  public async searchSkills(params: {
    query: string
    top_k?: number
  }): Promise<{ query: string, count: number, results: unknown[] }> {
    try {
      const q = params.query.trim()
      const topK = params.top_k || 8
      return await this.request<{ query: string, count: number, results: unknown[] }>(
        `/skills/search?q=${encodeURIComponent(q)}&top_k=${topK}`,
        { method: 'GET' }
      )
    } catch (error) {
      return {
        query: params.query,
        count: 0,
        results: []
      }
    }
  }

  /**
   * Query Supergent health.
   */
  public async getHealth(): Promise<Record<string, unknown>> {
    try {
      return await this.request<Record<string, unknown>>('/health', { method: 'GET' })
    } catch (error) {
      return {
        status: 'unreachable',
        error: (error as Error).message
      }
    }
  }
}
