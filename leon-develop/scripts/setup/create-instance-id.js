import fs from 'node:fs'

import { LEON_FILE_PATH } from '@/constants'
import { Telemetry } from '@/telemetry'

import { createSetupStatus } from './setup-status'

export default async () => {
  const status = createSetupStatus('Checking instance ID...').start()

  try {
    const { instanceID, birthDate } = await Telemetry.postInstall()

    if (!fs.existsSync(LEON_FILE_PATH)) {
      await fs.promises.writeFile(
        LEON_FILE_PATH,
        JSON.stringify(
          {
            instanceID,
            birthDate
          },
          null,
          2
        )
      )

      status.succeed('Instance ID: ready')
    } else {
      status.succeed('Instance ID: ready')
    }
  } catch (e) {
    status.warn(`Failed to create the instance ID: ${e}`)
  }
}
