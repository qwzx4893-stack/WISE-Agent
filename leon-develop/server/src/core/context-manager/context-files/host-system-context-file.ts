import os from 'node:os'
import path from 'node:path'

import { DateHelper } from '@/helpers/date-helper'
import { ContextFile } from '@/core/context-manager/context-file'
import { ContextProbeHelper } from '@/core/context-manager/context-probe-helper'

export class HostSystemContextFile extends ContextFile {
  public readonly filename = 'HOST_SYSTEM.md'
  public readonly ttlMs: number

  public constructor(
    private readonly probeHelper: ContextProbeHelper,
    ttlMs: number
  ) {
    super()
    this.ttlMs = ttlMs
  }

  public generate(): string {
    const username = this.probeHelper.getSafeUsername()
    const homeDirectory = os.homedir()
    const desktopPath = path.join(homeDirectory, 'Desktop')
    const downloadsPath = path.join(homeDirectory, 'Downloads')
    const operatingSystemNameVersion =
      this.probeHelper.getOperatingSystemNameVersion()
    const shell =
      process.env['SHELL'] ||
      process.env['COMSPEC'] ||
      process.env['ComSpec'] ||
      'unknown'
    const locale = Intl.DateTimeFormat().resolvedOptions().locale || 'unknown'
    const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'unknown'
    const ownerLocation = this.probeHelper.probeOwnerLocation({
      timeZone,
      locale
    })
    const vpnProxyStatus = this.probeHelper.probeVpnOrProxyStatus()
    const cpuModel = os.cpus()[0]?.model || 'unknown'
    const cpuCores = os.cpus().length
    const totalMemory = this.probeHelper.formatGiB(os.totalmem())
    const bootTime = DateHelper.getDateTime(Date.now() - os.uptime() * 1_000)

    return [
      `> OS/runtime identity, locale/timezone, VPN/proxy, hardware basics and common user directories. Host system is ${operatingSystemNameVersion} (${os.platform()} ${os.release()}, ${os.arch()}), user ${username}, home ${homeDirectory}, Desktop ${desktopPath}, Downloads ${downloadsPath}, shell ${shell}, runtime location hint ${ownerLocation.value}${vpnProxyStatus.behindVpnOrProxy ? ' (VPN/proxy detected).' : '.'}`,
      '# HOST_SYSTEM',
      `- Generated at: ${DateHelper.getDateTime()}`,
      `- OS name and version: ${operatingSystemNameVersion}`,
      `- Platform: ${os.platform()}`,
      `- OS type: ${os.type()}`,
      `- OS release: ${os.release()}`,
      `- Architecture: ${os.arch()}`,
      `- Hostname: ${os.hostname()}`,
      `- Locale: ${locale}`,
      `- Time zone: ${timeZone}`,
      `- Runtime location hint: ${ownerLocation.value}`,
      `- Runtime location hint source: ${ownerLocation.source}`,
      `- Runtime location hint confidence: ${ownerLocation.confidence}`,
      `- VPN/proxy detected: ${vpnProxyStatus.behindVpnOrProxy ? 'yes' : 'no'}`,
      `- VPN/proxy reasons: ${vpnProxyStatus.reasons.join(', ') || 'none'}`,
      `- VPN tunnel interfaces: ${vpnProxyStatus.tunnelInterfaces.join(', ') || 'none'}`,
      `- VPN-related processes: ${vpnProxyStatus.vpnProcesses.join(', ') || 'none'}`,
      `- CPU model: ${cpuModel}`,
      `- CPU cores: ${cpuCores}`,
      `- Total RAM: ${totalMemory}`,
      `- Username: ${username}`,
      `- Home directory: ${homeDirectory}`,
      `- Desktop path: ${desktopPath}`,
      `- Downloads path: ${downloadsPath}`,
      `- Shell: ${shell}`,
      `- Boot time: ${bootTime}`,
      `- Uptime: ${this.probeHelper.formatUptime(os.uptime())}`,
      `- Temporary directory: ${os.tmpdir()}`
    ].join('\n')
  }
}
