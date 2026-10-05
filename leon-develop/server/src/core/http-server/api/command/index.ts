import type { FastifyPluginAsync } from 'fastify'

import { postCommand } from '@/core/http-server/api/command/post'
import type { APIOptions } from '@/core/http-server/http-server'

export const commandPlugin: FastifyPluginAsync<APIOptions> = async (
  fastify,
  options
) => {
  await fastify.register(postCommand, options)
}
