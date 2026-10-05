import fs from 'node:fs'
import path from 'node:path'

import {
  NVIDIA_LIBS_PATH,
  NVIDIA_CUBLAS_PATH,
  NVIDIA_CUDNN_PATH,
  NVIDIA_CUDA_CUDART_PATH,
  NVIDIA_CUDA_CUPTI_PATH,
  NVIDIA_CUSPARSE_PATH,
  NVIDIA_CUSPARSELT_PATH,
  NVIDIA_CUSPARSE_FULL_PATH,
  NVIDIA_NCCL_PATH,
  NVIDIA_NVSHMEM_PATH,
  NVIDIA_NVJITLINK_PATH,
  NVIDIA_CUBLAS_MANIFEST_PATH,
  NVIDIA_CUDNN_MANIFEST_PATH,
  NVIDIA_CUDA_CUDART_MANIFEST_PATH,
  NVIDIA_CUDA_CUPTI_MANIFEST_PATH,
  NVIDIA_CUSPARSE_MANIFEST_PATH,
  NVIDIA_CUSPARSE_FULL_MANIFEST_PATH,
  NVIDIA_NCCL_MANIFEST_PATH,
  NVIDIA_NVSHMEM_MANIFEST_PATH,
  NVIDIA_NVJITLINK_MANIFEST_PATH,
  NVIDIA_CUDA_VERSION,
  NVIDIA_CUBLAS_VERSION,
  NVIDIA_CUDNN_VERSION,
  NVIDIA_CUDA_CUDART_VERSION,
  NVIDIA_CUDA_CUPTI_VERSION,
  NVIDIA_CUSPARSE_VERSION,
  NVIDIA_CUSPARSE_FULL_VERSION,
  NVIDIA_NCCL_VERSION,
  NVIDIA_NVSHMEM_VERSION,
  NVIDIA_NVJITLINK_VERSION,
  PYTORCH_NVIDIA_PATH,
  PYTORCH_TORCH_PATH
} from '@/constants'
import { FileHelper } from '@/helpers/file-helper'
import { SystemHelper } from '@/helpers/system-helper'

import { createSetupStatus } from './setup-status'

const { type: OS_TYPE, cpuArchitecture: CPU_ARCH } =
  SystemHelper.getInformation()
const NVIDIA_LIBRARY_LABELS = {
  cublas: 'cuBLAS',
  cudnn: 'cuDNN',
  cuda_cudart: 'CUDA Runtime',
  cuda_cupti: 'CUDA CUPTI',
  cusparse: 'cuSPARSE Lt',
  cusparse_full: 'cuSPARSE',
  nccl: 'NCCL',
  nvshmem: 'NVSHMEM',
  nvjitlink: 'nvJitLink'
}

/**
 * Map CPU architecture to NVIDIA's architecture naming convention
 */
function mapToNvidiaArch(cpuArch) {
  // Map Node.js process.arch values to NVIDIA naming
  if (cpuArch === 'arm64' || cpuArch === 'aarch64') {
    return 'aarch64'
  }
  if (cpuArch === 'x64' || cpuArch === 'x86_64') {
    return 'x86_64'
  }

  return 'x86_64'
}

async function ensureDirectoryLink(linkPath, targetPath) {
  if (!fs.existsSync(targetPath)) {
    return
  }

  await fs.promises.rm(linkPath, { recursive: true, force: true })
  await fs.promises.mkdir(path.dirname(linkPath), { recursive: true })

  const relativeTarget = path.relative(path.dirname(linkPath), targetPath)
  const linkType = SystemHelper.isWindows() ? 'junction' : 'dir'

  await fs.promises.symlink(relativeTarget, linkPath, linkType)
}

async function ensureCompatibilityLinks() {
  await ensureDirectoryLink(NVIDIA_CUSPARSELT_PATH, NVIDIA_CUSPARSE_PATH)
  await ensureDirectoryLink(
    path.join(NVIDIA_LIBS_PATH, 'cuda_runtime'),
    NVIDIA_CUDA_CUDART_PATH
  )

  if (fs.existsSync(PYTORCH_TORCH_PATH)) {
    await ensureDirectoryLink(PYTORCH_NVIDIA_PATH, NVIDIA_LIBS_PATH)
  }
}

/**
 * Read manifest file to get installed version
 */
function readManifest(manifestPath) {
  if (!fs.existsSync(manifestPath)) {
    return null
  }

  try {
    const content = fs.readFileSync(manifestPath, 'utf-8')

    return JSON.parse(content)
  } catch {
    return null
  }
}

/**
 * Get download URL for NVIDIA libraries
 */
function getNVIDIADownloadURL(library, version) {
  const ext = SystemHelper.isWindows() ? 'zip' : 'tar.xz'
  const arch = mapToNvidiaArch(CPU_ARCH)

  // NVIDIA CDN URLs for CUDA libraries and more
  if (library === 'cublas') {
    return `https://developer.download.nvidia.com/compute/cuda/redist/libcublas/${OS_TYPE}-${arch}/libcublas-${OS_TYPE}-${arch}-${version}-archive.${ext}`
  } else if (library === 'cudnn') {
    return `https://developer.download.nvidia.com/compute/cudnn/redist/cudnn/${OS_TYPE}-${arch}/cudnn-${OS_TYPE}-${arch}-${version}_cuda${NVIDIA_CUDA_VERSION}-archive.${ext}`
  } else if (library === 'cuda_cudart') {
    return `https://developer.download.nvidia.com/compute/cuda/redist/cuda_cudart/${OS_TYPE}-${arch}/cuda_cudart-${OS_TYPE}-${arch}-${version}-archive.${ext}`
  } else if (library === 'cuda_cupti') {
    return `https://developer.download.nvidia.com/compute/cuda/redist/cuda_cupti/${OS_TYPE}-${arch}/cuda_cupti-${OS_TYPE}-${arch}-${version}-archive.${ext}`
  } else if (library === 'cusparse') {
    return `https://developer.download.nvidia.com/compute/cusparselt/redist/libcusparse_lt/${OS_TYPE}-${arch}/libcusparse_lt-${OS_TYPE}-${arch}-${version}_cuda${NVIDIA_CUDA_VERSION}-archive.${ext}`
  } else if (library === 'cusparse_full') {
    return `https://developer.download.nvidia.com/compute/cuda/redist/libcusparse/${OS_TYPE}-${arch}/libcusparse-${OS_TYPE}-${arch}-${version}-archive.${ext}`
  } else if (library === 'nccl') {
    // NCCL is only available on Linux x86_64
    if (!SystemHelper.isLinux() || arch !== 'x86_64') {
      throw new Error('NCCL is only available on Linux x86_64')
    }

    return `https://developer.download.nvidia.com/compute/nccl/redist/nccl/${OS_TYPE}-${arch}/nccl-${OS_TYPE}-${arch}-${version}-archive.${ext}`
  } else if (library === 'nvshmem') {
    // NVSHMEM is only available on Linux x86_64
    if (!SystemHelper.isLinux() || arch !== 'x86_64') {
      throw new Error('NVSHMEM is only available on Linux x86_64')
    }

    return `https://developer.download.nvidia.com/compute/nvshmem/redist/libnvshmem/${OS_TYPE}-${arch}/libnvshmem-${OS_TYPE}-${arch}-${version}_cuda${NVIDIA_CUDA_VERSION}-archive.${ext}`
  } else if (library === 'nvjitlink') {
    return `https://developer.download.nvidia.com/compute/cuda/redist/libnvjitlink/${OS_TYPE}-${arch}/libnvjitlink-${OS_TYPE}-${arch}-${version}-archive.${ext}`
  }
}

/**
 * Install NVIDIA libraries if needed
 */
async function installNVIDIALibrary(
  library,
  requiredVersion,
  targetPath,
  manifestPath
) {
  const libraryLabel = NVIDIA_LIBRARY_LABELS[library] || library
  const status = createSetupStatus(`Setting up ${libraryLabel}...`).start()
  const manifest = readManifest(manifestPath)

  if (!manifest || manifest.version !== requiredVersion) {
    const ext = SystemHelper.isWindows() ? 'zip' : 'tar.xz'
    const archivePath = path.join(
      NVIDIA_LIBS_PATH,
      `${library}-${requiredVersion}.${ext}`
    )

    // Clean up old version
    await fs.promises.rm(targetPath, { recursive: true, force: true })
    await fs.promises.rm(archivePath, { force: true })

    // Create target directory
    await fs.promises.mkdir(targetPath, { recursive: true })

    try {
      const downloadURL = getNVIDIADownloadURL(library, requiredVersion)
      status.pause()

      await FileHelper.downloadFile(downloadURL, archivePath, {
        cliProgress: true,
        parallelStreams: 3,
        skipExisting: false
      })
      status.text = `Installing ${libraryLabel}...`
      status.start()

      // Extract archive using unified method
      await FileHelper.extractArchive(archivePath, targetPath, {
        stripComponents: 1
      })

      // Clean up and create manifest
      await Promise.all([
        fs.promises.rm(archivePath, { force: true }),
        FileHelper.createManifestFile(manifestPath, library, requiredVersion, {
          os: SystemHelper.getInformation().type,
          architecture: SystemHelper.getInformation().cpuArchitecture
        })
      ])
      status.succeed(`${libraryLabel} ${requiredVersion} ready`)
    } catch (error) {
      if (status.isSpinning) {
        status.fail(`Failed to set up ${libraryLabel}`)
      }
      throw new Error(
        `${libraryLabel} may require manual download from https://developer.nvidia.com/cuda-downloads: ${error}`
      )
    }
  } else {
    status.succeed(`${libraryLabel}: ${requiredVersion}`)
  }
}

/**
 * Main setup function
 */
async function setupNVIDIALibs() {
  // Skip on macOS since there is no CUDA involved
  if (SystemHelper.isMacOS()) {
    return
  }
  const status = createSetupStatus('Checking CUDA runtime support...').start()

  try {
    const { getLlama, LlamaLogLevel } = await Function(
      'return import("node-llama-cpp")'
    )()
    const llama = await getLlama({
      logLevel: LlamaLogLevel.disabled
    })

    const hasGPU = await SystemHelper.hasGPU(llama)

    if (!hasGPU) {
      status.succeed('CUDA runtime: skipped')
      return
    }

    status.succeed('CUDA runtime: detected')

    // Install/update cuBLAS
    await installNVIDIALibrary(
      'cublas',
      NVIDIA_CUBLAS_VERSION,
      NVIDIA_CUBLAS_PATH,
      NVIDIA_CUBLAS_MANIFEST_PATH
    )

    // Install/update cuDNN
    await installNVIDIALibrary(
      'cudnn',
      NVIDIA_CUDNN_VERSION,
      NVIDIA_CUDNN_PATH,
      NVIDIA_CUDNN_MANIFEST_PATH
    )

    // Install/update CUDA cudart runtime
    await installNVIDIALibrary(
      'cuda_cudart',
      NVIDIA_CUDA_CUDART_VERSION,
      NVIDIA_CUDA_CUDART_PATH,
      NVIDIA_CUDA_CUDART_MANIFEST_PATH
    )

    // Install/update CUDA CUPTI
    await installNVIDIALibrary(
      'cuda_cupti',
      NVIDIA_CUDA_CUPTI_VERSION,
      NVIDIA_CUDA_CUPTI_PATH,
      NVIDIA_CUDA_CUPTI_MANIFEST_PATH
    )

    // Install/update cuSPARSE-Lt (Linux only, both x86_64 and aarch64)
    if (SystemHelper.isLinux()) {
      try {
        await installNVIDIALibrary(
          'cusparse',
          NVIDIA_CUSPARSE_VERSION,
          NVIDIA_CUSPARSE_PATH,
          NVIDIA_CUSPARSE_MANIFEST_PATH
        )
      } catch (error) {
        status.warn(`cuSPARSE Lt skipped: ${error.message}`)
      }
    }

    // Install/update cuSPARSE (Linux only, both x86_64 and aarch64)
    if (SystemHelper.isLinux()) {
      try {
        await installNVIDIALibrary(
          'cusparse_full',
          NVIDIA_CUSPARSE_FULL_VERSION,
          NVIDIA_CUSPARSE_FULL_PATH,
          NVIDIA_CUSPARSE_FULL_MANIFEST_PATH
        )
      } catch (error) {
        status.warn(`cuSPARSE skipped: ${error.message}`)
      }
    }

    // Install/update nvJitLink (Linux only)
    if (SystemHelper.isLinux()) {
      try {
        await installNVIDIALibrary(
          'nvjitlink',
          NVIDIA_NVJITLINK_VERSION,
          NVIDIA_NVJITLINK_PATH,
          NVIDIA_NVJITLINK_MANIFEST_PATH
        )
      } catch (error) {
        status.warn(`nvJitLink skipped: ${error.message}`)
      }
    }

    // Install/update NCCL (Linux x86_64 only)
    if (SystemHelper.isLinux() && mapToNvidiaArch(CPU_ARCH) === 'x86_64') {
      try {
        await installNVIDIALibrary(
          'nccl',
          NVIDIA_NCCL_VERSION,
          NVIDIA_NCCL_PATH,
          NVIDIA_NCCL_MANIFEST_PATH
        )
      } catch (error) {
        status.warn(`NCCL skipped: ${error.message}`)
      }
    }

    // Install/update NVSHMEM (Linux x86_64 only)
    if (SystemHelper.isLinux() && mapToNvidiaArch(CPU_ARCH) === 'x86_64') {
      try {
        await installNVIDIALibrary(
          'nvshmem',
          NVIDIA_NVSHMEM_VERSION,
          NVIDIA_NVSHMEM_PATH,
          NVIDIA_NVSHMEM_MANIFEST_PATH
        )
      } catch (error) {
        status.warn(`NVSHMEM skipped: ${error.message}`)
      }
    }

    await ensureCompatibilityLinks()
  } catch (error) {
    if (status.isSpinning) {
      status.fail('Failed to inspect CUDA runtime support')
    }
    throw error
  }
}

export default setupNVIDIALibs
