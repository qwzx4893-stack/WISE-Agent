import AISDKRemoteLLMProvider from '@/core/llm-manager/llm-providers/ai-sdk-remote-llm-provider'
import type { ResolvedLLMTarget } from '@/core/llm-manager/llm-routing'

/**
 * @see https://inference-docs.cerebras.ai/api-reference/chat-completions
 */
export default class CerebrasLLMProvider extends AISDKRemoteLLMProvider {
  constructor(target: ResolvedLLMTarget) {
    super({
      name: 'Cerebras LLM Provider',
      providerName: 'cerebras',
      apiKeyEnv: 'LEON_CEREBRAS_API_KEY',
      model: target.model,
      baseURL: 'https://api.cerebras.ai/v1',
      flavor: 'cerebras'
    })
  }
}
