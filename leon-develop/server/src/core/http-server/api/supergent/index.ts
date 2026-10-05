import type { FastifyPluginAsync } from 'fastify'
import type { APIOptions } from '@/core/http-server/http-server'
import { LogHelper } from '@/helpers/log-helper'

const SUPERGENT_BASE_URL = (process.env['SUPERGENT_URL'] || 'http://127.0.0.1:8765').replace(/\/+$/, '')

export const supergentPlugin: FastifyPluginAsync<APIOptions> = async (
  fastify,
  options
) => {
  const prefix = `/api/${options.apiVersion}/supergent`

  // 1. Health check proxy
  fastify.route({
    method: 'GET',
    url: `${prefix}/health`,
    handler: async (_request, reply) => {
      try {
        const response = await fetch(`${SUPERGENT_BASE_URL}/health`)
        const data = await response.json()
        return reply.status(response.status).send(data)
      } catch (error) {
        LogHelper.error(`Supergent health check failed: ${(error as Error).message}`)
        return reply.status(503).send({
          status: 'unavailable',
          error: (error as Error).message
        })
      }
    }
  })

  // 2. Tools list proxy
  fastify.route({
    method: 'GET',
    url: `${prefix}/tools`,
    handler: async (_request, reply) => {
      try {
        const response = await fetch(`${SUPERGENT_BASE_URL}/tools`)
        const data = await response.json()
        return reply.status(response.status).send(data)
      } catch (error) {
        return reply.status(502).send({ error: (error as Error).message })
      }
    }
  })

  // 3. Skills list proxy
  fastify.route({
    method: 'GET',
    url: `${prefix}/skills`,
    handler: async (_request, reply) => {
      try {
        const response = await fetch(`${SUPERGENT_BASE_URL}/skills`)
        const data = await response.json()
        return reply.status(response.status).send(data)
      } catch (error) {
        return reply.status(502).send({ error: (error as Error).message })
      }
    }
  })

  // 3b. Skills search proxy
  fastify.route({
    method: 'GET',
    url: `${prefix}/skills/search`,
    handler: async (request, reply) => {
      try {
        const query = (request.query as Record<string, string>)?.['q'] || ''
        const response = await fetch(`${SUPERGENT_BASE_URL}/skills/search?q=${encodeURIComponent(query)}`)
        const data = await response.json()
        return reply.status(response.status).send(data)
      } catch (error) {
        return reply.status(502).send({ error: (error as Error).message })
      }
    }
  })

  // 3c. RAG knowledge search proxy
  fastify.route({
    method: 'GET',
    url: `${prefix}/knowledge/search`,
    handler: async (request, reply) => {
      try {
        const query = (request.query as Record<string, string>)?.['query'] || ''
        const sources = (request.query as Record<string, string>)?.['sources'] || 'wikipedia,arxiv,pubmed'
        const response = await fetch(`${SUPERGENT_BASE_URL}/knowledge/search?query=${encodeURIComponent(query)}&sources=${encodeURIComponent(sources)}`)
        const data = await response.json()
        return reply.status(response.status).send(data)
      } catch (error) {
        return reply.status(502).send({ error: (error as Error).message })
      }
    }
  })

  // 4. Chat proxy
  fastify.route({
    method: 'POST',
    url: `${prefix}/chat`,
    handler: async (request, reply) => {
      try {
        const response = await fetch(`${SUPERGENT_BASE_URL}/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(request.body)
        })
        const data = await response.json()
        return reply.status(response.status).send(data)
      } catch (error) {
        return reply.status(502).send({ error: (error as Error).message })
      }
    }
  })
}
