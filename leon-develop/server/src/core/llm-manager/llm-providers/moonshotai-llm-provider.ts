import AISDKRemoteLLMProvider from '@/core/llm-manager/llm-providers/ai-sdk-remote-llm-provider'
import type { ResolvedLLMTarget } from '@/core/llm-manager/llm-routing'

export default class MoonshotAILLMProvider extends AISDKRemoteLLMProvider {
  constructor(target: ResolvedLLMTarget) {
    super({
      name: 'MoonshotAI LLM Provider',
      providerName: 'moonshotai',
      apiKeyEnv: 'LEON_MOONSHOTAI_API_KEY',
      model: target.model,
      baseURL: 'https://api.moonshot.ai/v1',
      flavor: 'moonshotai'
    })
  }
}
