import path from 'node:path'

import { LoaderHelper } from '@/helpers/loader-helper'
import { LogHelper } from '@/helpers/log-helper'
import { SkillDomainHelper } from '@/helpers/skill-domain-helper'

import syncSkillDependencies from './setup/setup-skills/sync-skill-dependencies'

/**
 * Sync a skill's local dependencies on demand.
 */
;(async () => {
  LoaderHelper.start()

  try {
    const givenSkill = process.argv
      .slice(2)
      .find((argument) => argument !== '--')
      ?.toLowerCase()

    if (!givenSkill) {
      LogHelper.error(
        'Missing skill name. The command should be: "npm run sync-skill-deps -- {skill}"'
      )
      process.exit(1)
    }

    const currentSkill = await SkillDomainHelper.getNewSkillConfig(givenSkill)
    const currentSkillConfigPath =
      SkillDomainHelper.getNewSkillConfigPath(givenSkill)

    if (!currentSkill || !currentSkillConfigPath) {
      LogHelper.error(`Unknown skill "${givenSkill}"`)
      process.exit(1)
    }

    await syncSkillDependencies(givenSkill, {
      path: path.dirname(currentSkillConfigPath),
      bridge: currentSkill.bridge
    })
  } catch (e) {
    LogHelper.error(`Failed to sync skill dependencies: ${e}`)
    process.exit(1)
  } finally {
    LoaderHelper.stop()
  }
})()
