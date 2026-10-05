import path from 'node:path'

import {
  UV_INSTALL_PATH,
  UV_MANIFEST_PATH,
  UV_VERSION
} from '@/constants'
import { CPUArchitectures } from '@/types'
import { SystemHelper } from '@/helpers/system-helper'

import { setupRuntimeBinary } from './setup-runtime-binary'

const { cpuArchitecture: CPU_ARCH } = SystemHelper.getInformation()

function getBinaryPath() {
  return SystemHelper.isWindows()
    ? path.join(UV_INSTALL_PATH, 'uv.exe')
    : path.join(UV_INSTALL_PATH, 'uv')
}

function getAssetFileName() {
  if (SystemHelper.isLinux()) {
    if (CPU_ARCH === CPUArchitectures.X64) {
      return 'uv-x86_64-unknown-linux-gnu.tar.gz'
    }

    if (CPU_ARCH === CPUArchitectures.ARM64) {
      return 'uv-aarch64-unknown-linux-gnu.tar.gz'
    }
  }

  if (SystemHelper.isMacOS()) {
    if (CPU_ARCH === CPUArchitectures.X64) {
      return 'uv-x86_64-apple-darwin.tar.gz'
    }

    if (CPU_ARCH === CPUArchitectures.ARM64) {
      return 'uv-aarch64-apple-darwin.tar.gz'
    }
  }

  if (SystemHelper.isWindows()) {
    return CPU_ARCH === CPUArchitectures.ARM64
      ? 'uv-aarch64-pc-windows-msvc.zip'
      : 'uv-x86_64-pc-windows-msvc.zip'
  }

  throw new Error(
    `Unsupported platform for uv: ${SystemHelper.getInformation().type} ${CPU_ARCH}`
  )
}

export default async function setupUV() {
  const assetFileName = getAssetFileName()

  await setupRuntimeBinary({
    name: 'uv',
    version: UV_VERSION,
    basePath: UV_INSTALL_PATH,
    installPath: UV_INSTALL_PATH,
    manifestPath: UV_MANIFEST_PATH,
    binaryPath: getBinaryPath(),
    downloadURL: `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${assetFileName}`,
    archiveFileName: assetFileName
  })
}
