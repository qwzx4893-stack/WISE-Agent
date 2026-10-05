import AISDKRemoteLLMProvider from '@/core/llm-manager/llm-providers/ai-sdk-remote-llm-provider'
import type { ResolvedLLMTarget } from '@/core/llm-manager/llm-routing'

export default class OpenAILLMProvider extends AISDKRemoteLLMProvider {
  constructor(target: ResolvedLLMTarget) {
    super({
      name: 'OpenAI LLM Provider',
      providerName: 'openai',
      apiKeyEnv: 'LEON_OPENAI_API_KEY',
      model: target.model,
      baseURL: 'https://api.openai.com/v1',
      flavor: 'openai-responses'
    })
  }
}
