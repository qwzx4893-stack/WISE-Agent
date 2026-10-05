import fs from 'node:fs'

import {
  NVIDIA_CUBLAS_VERSION,
  NVIDIA_CUDA_VERSION,
  NVIDIA_CUDNN_VERSION,
  NVIDIA_CUSPARSE_FULL_VERSION,
  NVIDIA_CUSPARSE_VERSION,
  NVIDIA_LIBS_PATH,
  NVIDIA_NCCL_VERSION,
  NVIDIA_NVJITLINK_VERSION,
  NVIDIA_NVSHMEM_VERSION,
  PYTORCH_TORCH_PATH,
  PYTORCH_VERSION
} from '@/constants'
import { DateHelper } from '@/helpers/date-helper'
import { ContextFile } from '@/core/context-manager/context-file'
import { ContextProbeHelper } from '@/core/context-manager/context-probe-helper'

export class GpuComputeContextFile extends ContextFile {
  public readonly filename = 'GPU_COMPUTE.md'
  public readonly ttlMs: number

  public constructor(
    private readonly probeHelper: ContextProbeHelper,
    ttlMs: number
  ) {
    super()
    this.ttlMs = ttlMs
  }

  public generate(): string {
    const gpuProbe = this.probeHelper.probeNvidiaSmi()
    const hasTorchRuntimePath = fs.existsSync(PYTORCH_TORCH_PATH)
    const hasNvidiaLibsPath = fs.existsSync(NVIDIA_LIBS_PATH)

    const summary =
      gpuProbe.gpus.length > 0
        ? `GPU context detected ${gpuProbe.gpus.length} NVIDIA GPU(s), CUDA ${NVIDIA_CUDA_VERSION}, PyTorch ${PYTORCH_VERSION}.`
        : `GPU context has no NVIDIA GPU detected by nvidia-smi, CUDA ${NVIDIA_CUDA_VERSION}, PyTorch ${PYTORCH_VERSION}.`

    const gpuLines =
      gpuProbe.gpus.length > 0
        ? gpuProbe.gpus.map(
            (gpu, index) =>
              `- GPU ${index + 1}: ${gpu.name} | VRAM: ${gpu.memoryMb} MB | Driver: ${gpu.driverVersion}`
          )
        : ['- GPU list: none detected']

    return [
      `> GPU inventory and compute runtime versions. ${summary}`,
      '# GPU_COMPUTE',
      `- Generated at: ${DateHelper.getDateTime()}`,
      `- Probe status: ${gpuProbe.status}`,
      ...gpuLines,
      `- CUDA version: ${NVIDIA_CUDA_VERSION}`,
      `- cuDNN version: ${NVIDIA_CUDNN_VERSION}`,
      `- cuBLAS version: ${NVIDIA_CUBLAS_VERSION}`,
      `- cuSPARSE version: ${NVIDIA_CUSPARSE_VERSION}`,
      `- cuSPARSE full version: ${NVIDIA_CUSPARSE_FULL_VERSION}`,
      `- NCCL version: ${NVIDIA_NCCL_VERSION}`,
      `- NVSHMEM version: ${NVIDIA_NVSHMEM_VERSION}`,
      `- NVJITLINK version: ${NVIDIA_NVJITLINK_VERSION}`,
      `- NVIDIA libs path: ${NVIDIA_LIBS_PATH} (${hasNvidiaLibsPath ? 'exists' : 'missing'})`,
      `- PyTorch version: ${PYTORCH_VERSION}`,
      `- PyTorch torch path: ${PYTORCH_TORCH_PATH} (${hasTorchRuntimePath ? 'exists' : 'missing'})`
    ].join('\n')
  }
}
