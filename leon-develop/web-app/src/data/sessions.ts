import sessionsData from './sessions.json'

export interface ConversationSession {
  id: string
  title: string
  isTitleGenerated: boolean
  isPinned: boolean
  createdAt: number
  updatedAt: number
  lastMessageAt: number | null
  messageCount: number
  modelTarget: string | null
}

export interface ConversationSessionIndex {
  activeSessionId: string
  sessions: ConversationSession[]
}

export const sessionIndex = sessionsData satisfies ConversationSessionIndex
