import ora from 'ora'

import { createSetupJokeScheduler } from './setup-jokes'

/**
 * Create a setup spinner that plays well with prompts and download output.
 */
export function createSetupStatus(text) {
  const spinner = ora({
    spinner: 'dots2',
    color: 'cyan',
    text,
    stream: process.stdout,
    discardStdin: false
  })
  const jokeScheduler = createSetupJokeScheduler()
  const status = {
    start() {
      jokeScheduler.start()
      spinner.start()

      return status
    },
    pause() {
      spinner.stop()

      return status
    },
    stop() {
      jokeScheduler.finish()
      spinner.stop()

      return status
    },
    succeed(successText) {
      jokeScheduler.finish()
      spinner.succeed(successText)

      return status
    },
    fail(failureText) {
      jokeScheduler.finish()
      spinner.fail(failureText)

      return status
    },
    warn(warningText) {
      jokeScheduler.finish()
      spinner.warn(warningText)

      return status
    },
    get isSpinning() {
      return spinner.isSpinning
    },
    get text() {
      return spinner.text
    },
    set text(nextText) {
      spinner.text = nextText
    }
  }

  return status
}
