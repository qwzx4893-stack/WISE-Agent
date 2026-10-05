import { DateHelper } from '@/helpers/date-helper'
import { ContextFile } from '@/core/context-manager/context-file'
import { ContextProbeHelper } from '@/core/context-manager/context-probe-helper'

export class StorageContextFile extends ContextFile {
  public readonly filename = 'STORAGE.md'
  public readonly ttlMs: number

  public constructor(
    private readonly probeHelper: ContextProbeHelper,
    ttlMs: number
  ) {
    super()
    this.ttlMs = ttlMs
  }

  public generate(): string {
    const probeResult = this.probeHelper.probeStorage()
    const generatedAt = DateHelper.getDateTime()

    const summary = probeResult.summary || 'Storage snapshot unavailable.'
    const entries =
      probeResult.entries.length > 0
        ? probeResult.entries.map(
            (entry) =>
              `- ${entry.filesystem}: total ${entry.size}, used ${entry.used}, free ${entry.available}, usage ${entry.usedPct}, mount ${entry.mountPoint}`
          )
        : ['- No storage entries collected']

    return [
      `> Filesystem totals, usage and mount points. ${summary}`,
      '# STORAGE',
      `- Generated at: ${generatedAt}`,
      `- Source: ${probeResult.source}`,
      ...entries
    ].join('\n')
  }
}
