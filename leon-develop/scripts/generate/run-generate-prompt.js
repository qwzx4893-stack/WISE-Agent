import { LogHelper } from '@/helpers/log-helper'

import generatePrompt from './generate-prompt'

/**
 * Execute the generating prompt script
 */
;(async () => {
  try {
    const templateName = process.argv[2]

    await generatePrompt(templateName)
  } catch (error) {
    LogHelper.error(`Failed to generate the prompt: ${error}`)
  }
})()
