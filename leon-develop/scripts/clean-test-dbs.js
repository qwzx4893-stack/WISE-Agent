import fs from 'node:fs'
import { join } from 'node:path'

import { LogHelper } from '@/helpers/log-helper'
import { SkillDomainHelper } from '@/helpers/skill-domain-helper'

/**
 * This script delete test DB files if they exist
 */
export default () =>
  new Promise(async (resolve, reject) => {
    LogHelper.info('Cleaning test DB files...')

    const skillNames = await SkillDomainHelper.listSkillFolders()

    for (const skillName of skillNames) {
      const skillConfigPath = SkillDomainHelper.getNewSkillConfigPath(skillName)
      if (!skillConfigPath) {
        continue
      }

      try {
        // TODO: handle case where the memory folder contain multiple DB nodes
        const dbFolder = join(skillConfigPath.replace(/\/skill\.json$/, ''), 'memory')
        const dbTestFiles = (await fs.promises.readdir(dbFolder)).filter(
          (entity) => entity.indexOf('.spec.json') !== -1
        )

        if (dbTestFiles.length > 0) {
          LogHelper.info(`Deleting ${dbTestFiles[0]}...`)
          await fs.promises.unlink(join(dbFolder, dbTestFiles[0]))
          LogHelper.success(`${dbTestFiles[0]} deleted`)
        }
      } catch (e) {
        LogHelper.error(`Failed to clean: "${skillName}" test DB file`)
        reject(e)
      }
    }

    LogHelper.success('Cleaning done')
    resolve()
  })
