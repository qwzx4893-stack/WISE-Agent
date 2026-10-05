import AISDKRemoteLLMProvider from '@/core/llm-manager/llm-providers/ai-sdk-remote-llm-provider'
import type { ResolvedLLMTarget } from '@/core/llm-manager/llm-routing'

/**
 * @see https://router.huggingface.co/v1/chat/completions
 */
export default class HuggingFaceLLMProvider extends AISDKRemoteLLMProvider {
  constructor(target: ResolvedLLMTarget) {
    super({
      name: 'HuggingFace LLM Provider',
      providerName: 'huggingface',
      apiKeyEnv: 'LEON_HUGGINGFACE_API_KEY',
      model: target.model,
      baseURL: 'https://router.huggingface.co/v1',
      flavor: 'huggingface'
    })
  }
}
