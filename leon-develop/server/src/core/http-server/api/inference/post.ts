import type { FastifyPluginAsync } from 'fastify'

import type { APIOptions } from '@/core/http-server/http-server'
import { LLM_MANAGER } from '@/core'
import { runInference } from '@/core/llm-manager/inference'

interface PostInferenceSchema {
  body: {
    prompt: string
    systemPrompt?: string
    temperature?: number
    maxTokens?: number
    thoughtTokensBudget?: number
    jsonSchema?: Record<string, unknown>
    disableThinking?: boolean
    reasoningMode?: 'off' | 'guarded' | 'on'
    trackProviderErrors?: boolean
  }
}

export const postInference: FastifyPluginAsync<APIOptions> = async (
  fastify,
  options
) => {
  fastify.route<{
    Body: PostInferenceSchema['body']
  }>({
    method: 'POST',
    url: `/api/${options.apiVersion}/inference`,
    handler: async (request, reply) => {
      if (!LLM_MANAGER.isLLMEnabled) {
        reply.statusCode = 400
        reply.send({
          success: false,
          status: reply.statusCode,
          code: 'llm_not_enabled',
          message: 'LLM is not enabled.'
        })
        return
      }

      const result = await runInference(request.body)
      if (!result) {
        reply.statusCode = 502
        reply.send({
          success: false,
          status: reply.statusCode,
          code: 'inference_failed',
          message: 'Inference failed.'
        })
        return
      }

      reply.send({
        success: true,
        status: 200,
        code: 'inference_executed',
        message: 'Inference executed.',
        ...result
      })
    }
  })
}
