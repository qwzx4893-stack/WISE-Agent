import fs from 'node:fs'

import { path as ffprobePath } from '@ffprobe-installer/ffprobe'

import { SystemHelper } from '@/helpers/system-helper'

import { createSetupStatus } from './setup-status'

export default async () => {
  const status = createSetupStatus('Checking ffprobe permissions...').start()

  try {
    if (SystemHelper.isWindows()) {
      status.succeed('ffprobe: ready')

      return
    }

    await fs.promises.chmod(ffprobePath, 0o755)

    status.succeed('ffprobe: ready')
  } catch (e) {
    status.warn(`Failed to set ffprobe permissions: ${e}`)
  }
}
