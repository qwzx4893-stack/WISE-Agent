import psList from 'ps-list'
import kill from 'tree-kill'

const LEON_PROCESS_PATTERNS = [
  'nodemon',
  'server/dist/index.js',
  'scripts/run-tcp-server.js',
  'tcp_server/src/main.py',
  'leon-tcp-server',
  'pt_main_thread'
]

/**
 * Kill a process tree by PID.
 */
function killProcess(pid) {
  return new Promise((resolve, reject) => {
    kill(pid, (error) => {
      if (error) {
        reject(error)
        return
      }

      resolve()
    })
  })
}

/**
 * Whether a process belongs to this Leon workspace.
 */
function isLeonProcess(processInfo, workspacePath) {
  const name = processInfo.name ?? ''
  const cmd = processInfo.cmd ?? ''

  if (
    processInfo.pid === process.pid ||
    processInfo.ppid === process.pid ||
    (!name && !cmd)
  ) {
    return false
  }

  if (name === 'leon' || cmd === 'leon') {
    return true
  }

  if (cmd.includes(workspacePath)) {
    return true
  }

  return LEON_PROCESS_PATTERNS.some((pattern) => {
    return name.includes(pattern) || cmd.includes(pattern)
  })
}

async function killLeonProcesses() {
  const workspacePath = process.cwd()
  const processList = await psList()
  const leonProcesses = processList.filter((processInfo) => {
    return isLeonProcess(processInfo, workspacePath)
  })

  if (leonProcesses.length === 0) {
    console.log('No Leon process is currently running.')
    return
  }

  for (const processInfo of leonProcesses) {
    try {
      await killProcess(processInfo.pid)
      console.log(`Killed PID ${processInfo.pid}: ${processInfo.name || processInfo.cmd}`)
    } catch (error) {
      console.error(
        `Failed to kill PID ${processInfo.pid}: ${
          error instanceof Error ? error.message : String(error)
        }`
      )
    }
  }
}

void killLeonProcesses()
