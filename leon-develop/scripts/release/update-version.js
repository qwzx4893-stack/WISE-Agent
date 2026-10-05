import fs from 'node:fs'

import { LogHelper } from '@/helpers/log-helper'

/**
 * Update version number in files which need version number
 */
export default (version) =>
  new Promise(async (resolve, reject) => {
    LogHelper.info('Updating version...')

    try {
      const packageJSONPath = 'package.json'
      const packageJSON = JSON.parse(
        await fs.promises.readFile(packageJSONPath, 'utf8')
      )
      packageJSON.version = version
      await fs.promises.writeFile(
        packageJSONPath,
        `${JSON.stringify(packageJSON, null, 2)}\n`
      )

      LogHelper.success(`Version updated to ${version}`)
      resolve()
    } catch (e) {
      LogHelper.error(`Error while updating version: ${e}`)
      reject(e)
    }
  })
