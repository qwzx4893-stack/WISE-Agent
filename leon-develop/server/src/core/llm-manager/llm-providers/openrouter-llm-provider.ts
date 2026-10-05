import AISDKRemoteLLMProvider from '@/core/llm-manager/llm-providers/ai-sdk-remote-llm-provider'
import type { ResolvedLLMTarget } from '@/core/llm-manager/llm-routing'

export default class OpenRouterLLMProvider extends AISDKRemoteLLMProvider {
  constructor(target: ResolvedLLMTarget) {
    super({
      name: 'OpenRouter LLM Provider',
      providerName: 'openrouter',
      apiKeyEnv: 'LEON_OPENROUTER_API_KEY',
      model: target.model,
      baseURL: 'https://openrouter.ai/api/v1',
      flavor: 'openrouter'
    })
  }
}
