import type { MessageLog } from '@/types'

function cleanLine(line: string): string {
  return line
    .replace(/\s+/g, ' ')
    .replace(/[^\S\r\n]+$/g, '')
    .trim()
}

function toBullet(text: string): string {
  const cleaned = cleanLine(text)
  if (!cleaned) {
    return ''
  }

  const truncated = cleaned.length > 180
    ? `${cleaned.slice(0, 177)}...`
    : cleaned

  return `- ${truncated}`
}

export function buildDailyMarkdownSummary(
  dayKey: string,
  conversationLogs: MessageLog[]
): string {
  const updatedAt = new Date().toISOString()
  const bullets: string[] = []

  const lastLogs = conversationLogs.slice(-40)
  for (const log of lastLogs) {
    const message = cleanLine(log.message)
    if (!message) {
      continue
    }

    const prefix = log.who === 'owner' ? 'Owner:' : 'Leon:'
    const bullet = toBullet(`${prefix} ${message}`)
    if (!bullet || bullets.includes(bullet)) {
      continue
    }

    bullets.push(bullet)
    if (bullets.length >= 18) {
      break
    }
  }

  const summaryLine =
    bullets.length > 0
      ? `> Daily memory for ${dayKey}. Captures the most recent key points of the conversation.`
      : `> Daily memory for ${dayKey}. No key points captured yet.`

  const body =
    bullets.length > 0
      ? bullets.join('\n')
      : '- No notable memory yet for this day.'

  return `${summaryLine}\n\n# ${dayKey}\n\nUpdated At: ${updatedAt}\n\n${body}\n`
}
