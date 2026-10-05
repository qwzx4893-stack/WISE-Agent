import { PYTHON_BRIDGE_SRC_PATH } from '@/constants'

import { setupPythonProjectEnv } from './setup-python-project-env'

/**
 * Sync the Python bridge runtime environment from its `pyproject.toml`.
 */
export default async function setupPythonBridgeEnv() {
  await setupPythonProjectEnv({
    name: 'Python bridge',
    projectPath: PYTHON_BRIDGE_SRC_PATH,
    stampFileName: '.last-python-bridge-deps-sync'
  })
}
