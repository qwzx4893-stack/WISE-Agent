import type { FastifyPluginAsync } from 'fastify'

import { getSystemWidgets } from '@/core/http-server/api/system-widgets/get'
import type { APIOptions } from '@/core/http-server/http-server'

export const systemWidgetsPlugin: FastifyPluginAsync<APIOptions> = async (
  fastify,
  options
) => {
  await fastify.register(getSystemWidgets, options)
}
