import os from 'node:os'

import { SystemHelper } from '@/helpers/system-helper'
import { DateHelper } from '@/helpers/date-helper'
import { ContextFile } from '@/core/context-manager/context-file'
import { ContextProbeHelper } from '@/core/context-manager/context-probe-helper'

export class SystemResourcesContextFile extends ContextFile {
  public readonly filename = 'SYSTEM_RESOURCES.md'
  public readonly ttlMs: number

  public constructor(
    private readonly probeHelper: ContextProbeHelper,
    ttlMs: number
  ) {
    super()
    this.ttlMs = ttlMs
  }

  public generate(): string {
    const generatedAt = DateHelper.getDateTime()
    const totalMemoryBytes = os.totalmem()
    const freeMemoryBytes = SystemHelper.getFreeRAMInBytes()
    const usedMemoryBytes = Math.max(totalMemoryBytes - freeMemoryBytes, 0)
    const usedMemoryPct =
      totalMemoryBytes > 0
        ? ((usedMemoryBytes / totalMemoryBytes) * 100).toFixed(1)
        : '0.0'
    const [load1 = 0, load5 = 0, load15 = 0] = os.loadavg()
    const loadAverageSummary = SystemHelper.isWindows()
      ? 'n/a'
      : `${load1.toFixed(2)}/${load5.toFixed(2)}/${load15.toFixed(2)}`

    return [
      `> Uptime, RAM usage and CPU load snapshot. Resource snapshot: uptime ${this.probeHelper.formatUptime(os.uptime())}, RAM used ${usedMemoryPct}%, load average ${loadAverageSummary}.`,
      '# SYSTEM_RESOURCES',
      `- Generated at: ${generatedAt}`,
      `- Uptime: ${this.probeHelper.formatUptime(os.uptime())}`,
      `- Boot time: ${DateHelper.getDateTime(Date.now() - os.uptime() * 1_000)}`,
      `- RAM total: ${this.probeHelper.formatGiB(totalMemoryBytes)}`,
      `- RAM used: ${this.probeHelper.formatGiB(usedMemoryBytes)} (${usedMemoryPct}%)`,
      `- RAM free: ${this.probeHelper.formatGiB(freeMemoryBytes)}`,
      `- Process RSS: ${this.probeHelper.formatGiB(process.memoryUsage().rss)}`,
      `- CPU cores: ${os.cpus().length}`,
      `- CPU load average (1m, 5m, 15m): ${
        SystemHelper.isWindows()
          ? 'not available on Windows'
          : `${load1.toFixed(2)}, ${load5.toFixed(2)}, ${load15.toFixed(2)}`
      }`
    ].join('\n')
  }
}
