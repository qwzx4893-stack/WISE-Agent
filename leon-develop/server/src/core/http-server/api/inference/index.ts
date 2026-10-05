import type { FastifyPluginAsync } from 'fastify'

import { postInference } from '@/core/http-server/api/inference/post'
import type { APIOptions } from '@/core/http-server/http-server'

export const inferencePlugin: FastifyPluginAsync<APIOptions> = async (
  fastify,
  options
) => {
  await fastify.register(postInference, options)
}
