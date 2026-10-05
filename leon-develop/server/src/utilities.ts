import fs from 'node:fs'

/**
 * Files
 */
export async function isFileEmpty(path: string): Promise<boolean> {
  return (await fs.promises.readFile(path)).length === 0
}

/**
 * Misc
 */
const TCP_SERVER_WARNINGS_TO_IGNORE = [
  'RuntimeWarning:',
  'FutureWarning:',
  'UserWarning:',
  '<00:00',
  '00:00<',
  'CUDNN_STATUS_NOT_SUPPORTED',
  'cls.seq_relationship.weight',
  'ALSA lib',
  'jack',
  'server request channel',
  '[W:onnxruntime:Default'
]
export function shouldIgnoreTCPServerError(error: string): boolean {
  return TCP_SERVER_WARNINGS_TO_IGNORE.some((warning) =>
    error.toLowerCase().includes(warning.toLowerCase())
  )
}
