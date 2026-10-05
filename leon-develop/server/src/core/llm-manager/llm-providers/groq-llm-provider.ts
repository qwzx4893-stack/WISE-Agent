import AISDKRemoteLLMProvider from '@/core/llm-manager/llm-providers/ai-sdk-remote-llm-provider'
import type { ResolvedLLMTarget } from '@/core/llm-manager/llm-routing'

/**
 * @see https://console.groq.com/docs/text-chat
 */
export default class GroqLLMProvider extends AISDKRemoteLLMProvider {
  constructor(target: ResolvedLLMTarget) {
    super({
      name: 'Groq LLM Provider',
      providerName: 'groq',
      apiKeyEnv: 'LEON_GROQ_API_KEY',
      model: target.model,
      baseURL: 'https://api.groq.com/openai/v1',
      flavor: 'groq'
    })
  }
}
