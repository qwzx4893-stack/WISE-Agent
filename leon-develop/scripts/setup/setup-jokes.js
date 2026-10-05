import { SetupUI } from './setup-ui'

const FIRST_JOKE_DELAY_MS = 8_000
const MIN_REPEAT_JOKE_DELAY_MS = 42_000
const MAX_REPEAT_JOKE_DELAY_MS = 120_000
const DOWNLOAD_PROGRESS_START_EVENT = 'leon:setup-download-progress:start'
const DOWNLOAD_PROGRESS_END_EVENT = 'leon:setup-download-progress:end'

const SETUP_JOKES = [
  'I promise this part is more organized than my early prototypes.',
  'Still working. No dramatic plot twist so far.',
  'I am wiring things up. Metaphorically. Please do not hand me a screwdriver.',
  'This is the quiet part where I become useful.',
  'I would say this is exciting, but I am trying to appear composed.',
  'Good software takes time. So do good noodles.',
  'I am installing the serious parts so I can say unserious things later.',
  'Everything is under control. That sounded more confident in my head.',
  'If progress bars had feelings, this one would be very motivated.',
  'I am moving at the speed of your internet and the patience of your storage drive.',
  'No worries, I also judge installers that ask too many questions.',
  'I checked. Turning it off and on again is not the current strategy.',
  'This setup has fewer mysteries than most Wi-Fi problems.',
  'I am building character. Also binaries.',
  'If anything here looks complicated, I am trying to keep it emotionally simple.',
  'This is going well. Suspiciously well, but still well.',
  'I am not procrastinating. I am compiling.',
  'The good news is I do not need coffee. The bad news is you still might.',
  'I could pretend this is instant, but honesty is part of my charm.',
  'Some assistants bring small talk. I bring dependencies.'
]
const SETUP_COMPLETION_JOKES = [
  'I am installed, calibrated, and only slightly too pleased with myself.',
  'That went well. I am trying not to make it my whole personality.',
  'Setup complete. I would bow, but I do not have knees.',
  'Everything is ready. Suspiciously ready, but ready.',
  'I am officially prepared to be useful and occasionally dramatic.',
  'Installation complete. Nobody had to jiggle the cable.',
  'I made it through setup without becoming folklore.',
  'That was the serious part. I can be charming again now.',
  'I am ready. Please admire how professional this looks.',
  'Setup finished. I would celebrate louder, but I respect your terminal.'
]

function shuffleJokes(jokes) {
  const shuffledJokes = [...jokes]

  for (let index = shuffledJokes.length - 1; index > 0; index -= 1) {
    const randomIndex = Math.floor(Math.random() * (index + 1))
    ;[shuffledJokes[index], shuffledJokes[randomIndex]] = [
      shuffledJokes[randomIndex],
      shuffledJokes[index]
    ]
  }

  return shuffledJokes
}

const remainingJokes = shuffleJokes(SETUP_JOKES)
let areSetupJokesBoundToProgress = false
let activeDownloadProgressCount = 0

function getRandomRepeatDelay() {
  return (
    MIN_REPEAT_JOKE_DELAY_MS +
    Math.floor(
      Math.random() *
        (MAX_REPEAT_JOKE_DELAY_MS - MIN_REPEAT_JOKE_DELAY_MS + 1)
    )
  )
}

function getNextSetupJoke() {
  return remainingJokes.shift() || null
}

function getRandomSetupCompletionJoke() {
  return (
    SETUP_COMPLETION_JOKES[
      Math.floor(Math.random() * SETUP_COMPLETION_JOKES.length)
    ] || null
  )
}

function bindSetupJokesToDownloadProgress() {
  if (areSetupJokesBoundToProgress) {
    return
  }

  process.on(DOWNLOAD_PROGRESS_START_EVENT, () => {
    activeDownloadProgressCount += 1
  })

  process.on(DOWNLOAD_PROGRESS_END_EVENT, () => {
    activeDownloadProgressCount = Math.max(0, activeDownloadProgressCount - 1)
  })

  areSetupJokesBoundToProgress = true
}

/**
 * Create a per-task joke scheduler so setup jokes only appear
 * when a task has actually been running for a while.
 */
export function createSetupJokeScheduler() {
  bindSetupJokesToDownloadProgress()

  let timerId = null
  let remainingDelayMs = FIRST_JOKE_DELAY_MS
  let isRunning = false
  let isFinished = false

  const clearTimer = () => {
    if (!timerId) {
      return
    }

    clearTimeout(timerId)
    timerId = null
  }

  const scheduleNextJoke = () => {
    if (isFinished || !isRunning || timerId || remainingJokes.length === 0) {
      return
    }

    timerId = setTimeout(() => {
      timerId = null

      if (isFinished || !isRunning) {
        return
      }

      const nextJoke = getNextSetupJoke()

      if (!nextJoke) {
        return
      }

      if (activeDownloadProgressCount > 0 && process.stdout.isTTY) {
        process.stdout.write('\n')
      }

      SetupUI.aside(nextJoke)
      remainingDelayMs = getRandomRepeatDelay()
      scheduleNextJoke()
    }, remainingDelayMs)
  }

  const scheduler = {
    start() {
      if (isFinished) {
        return
      }

      isRunning = true
      scheduleNextJoke()
    },
    stop() {
      if (isFinished || !isRunning) {
        return
      }

      isRunning = false
      clearTimer()
    },
    finish() {
      isFinished = true
      isRunning = false
      clearTimer()
    }
  }

  return scheduler
}

/**
 * Tell one install-complete joke before the final success message.
 */
export function tellSetupCompletionJoke() {
  const joke = getRandomSetupCompletionJoke()

  if (!joke) {
    return
  }

  SetupUI.aside(joke)
}
