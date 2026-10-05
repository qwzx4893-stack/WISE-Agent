import fs from 'node:fs'
import path from 'node:path'

import { FileHelper } from '@/helpers/file-helper'
import { SystemHelper } from '@/helpers/system-helper'

import { createSetupStatus } from './setup-status'

const MOVE_FALLBACK_ERROR_CODES = new Set(['EXDEV', 'EPERM', 'EBUSY', 'EACCES'])

function readManifest(manifestPath) {
  if (!fs.existsSync(manifestPath)) {
    return null
  }

  try {
    return JSON.parse(fs.readFileSync(manifestPath, 'utf8'))
  } catch {
    return null
  }
}

async function removePath(targetPath) {
  await fs.promises.rm(targetPath, { recursive: true, force: true })
}

function isMoveFallbackError(error) {
  return (
    error instanceof Error &&
    'code' in error &&
    MOVE_FALLBACK_ERROR_CODES.has(error.code)
  )
}

async function movePath(sourcePath, destinationPath) {
  try {
    await fs.promises.rename(sourcePath, destinationPath)
  } catch (error) {
    if (!isMoveFallbackError(error)) {
      throw error
    }

    await fs.promises.cp(sourcePath, destinationPath, {
      recursive: true,
      force: true
    })
    await removePath(sourcePath)
  }
}

async function moveDirectoryContents(sourcePath, destinationPath) {
  await fs.promises.mkdir(destinationPath, { recursive: true })

  const entries = await fs.promises.readdir(sourcePath, {
    withFileTypes: true
  })

  await Promise.all(
    entries.map((entry) =>
      movePath(
        path.join(sourcePath, entry.name),
        path.join(destinationPath, entry.name)
      )
    )
  )
}

async function cleanInstallDirectory(basePath) {
  await fs.promises.mkdir(basePath, { recursive: true })

  const entries = await fs.promises.readdir(basePath, {
    withFileTypes: true
  })

  await Promise.all(
    entries
      .filter((entry) => entry.name !== 'versions.json')
      .map((entry) => removePath(path.join(basePath, entry.name)))
  )
}

async function getFlattenedExtractionRoot(extractionPath) {
  const entries = await fs.promises.readdir(extractionPath, {
    withFileTypes: true
  })

  if (entries.length === 1 && entries[0]?.isDirectory()) {
    return path.join(extractionPath, entries[0].name)
  }

  return extractionPath
}

/**
 * Install a portable runtime into Leon's managed `bin/` directory from either
 * an archive asset or a direct executable download.
 */
export async function setupRuntimeBinary({
  name,
  version,
  basePath,
  installPath,
  manifestPath,
  binaryPath,
  downloadURL,
  archiveFileName = null
}) {
  const status = createSetupStatus(`Downloading and setting up ${name}...`).start()
  const manifest = readManifest(manifestPath)

  if (manifest?.version === version && fs.existsSync(binaryPath)) {
    status.succeed(`${name}: ${version}`)

    return
  }

  await cleanInstallDirectory(basePath)

  try {
    status.pause()

    if (archiveFileName) {
      const archivePath = path.join(basePath, archiveFileName)
      const extractionPath = await fs.promises.mkdtemp(
        path.join(basePath, `${name.toLowerCase()}-extract-`)
      )

      try {
        await FileHelper.downloadFile(downloadURL, archivePath, {
          cliProgress: true,
          parallelStreams: 3,
          skipExisting: false
        })
        status.text = `Installing ${name}...`
        status.start()

        await FileHelper.extractArchive(archivePath, extractionPath)

        const flattenedRootPath = await getFlattenedExtractionRoot(extractionPath)

        await moveDirectoryContents(flattenedRootPath, installPath)
      } finally {
        await Promise.all([
          fs.promises.rm(archivePath, { force: true }),
          fs.promises.rm(extractionPath, { recursive: true, force: true })
        ])
      }
    } else {
      await fs.promises.mkdir(path.dirname(binaryPath), { recursive: true })

      await FileHelper.downloadFile(downloadURL, binaryPath, {
        cliProgress: true,
        parallelStreams: 3,
        skipExisting: false
      })
      status.text = `Finalizing ${name}...`
      status.start()
    }

    if (!SystemHelper.isWindows()) {
      await fs.promises.chmod(binaryPath, 0o755)
    }

    if (!fs.existsSync(binaryPath)) {
      throw new Error(`Cannot find ${name} binary at "${binaryPath}"`)
    }

    await FileHelper.createManifestFile(manifestPath, name.toLowerCase(), version, {
      os: SystemHelper.getInformation().type,
      architecture: SystemHelper.getInformation().cpuArchitecture
    })

    status.succeed(`${name}: ${version}`)
  } catch (error) {
    if (status.isSpinning) {
      status.fail(`Failed to set up ${name}`)
    }

    throw new Error(`Failed to set up ${name}: ${error}`)
  }
}
