import type { FastifyPluginAsync } from 'fastify'

import { getConversationHistory } from '@/core/http-server/api/conversation-history/get'
import type { APIOptions } from '@/core/http-server/http-server'

export const conversationHistoryPlugin: FastifyPluginAsync<APIOptions> = async (
  fastify,
  options
) => {
  await fastify.register(getConversationHistory, options)
}
