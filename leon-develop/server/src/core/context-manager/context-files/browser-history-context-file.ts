import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { DateHelper } from '@/helpers/date-helper'
import { SystemHelper } from '@/helpers/system-helper'
import { ContextFile } from '@/core/context-manager/context-file'
import { ContextProbeHelper } from '@/core/context-manager/context-probe-helper'

type BrowserHistoryFlavor = 'chromium' | 'firefox' | 'safari'

interface BrowserHistoryDatabase {
  browser: string
  profile: string
  flavor: BrowserHistoryFlavor
  filePath: string
}

interface BrowserHistoryEntry {
  browser: string
  profile: string
  url: string
  title: string
  visitedAt: string
}

interface BrowserHistoryProbeResult {
  source: string
  checkedDatabasesCount: number
  selectedBrowser: string
  selectedProfile: string
  entries: BrowserHistoryEntry[]
}

const MAX_DATABASES_TO_QUERY = 6
const MAX_OUTPUT_ENTRIES = 64
const MAX_URL_CHARS = 128

export class BrowserHistoryContextFile extends ContextFile {
  public readonly filename = 'BROWSER_HISTORY.md'
  public readonly ttlMs: number

  public constructor(
    private readonly probeHelper: ContextProbeHelper,
    ttlMs: number
  ) {
    super()
    this.ttlMs = ttlMs
  }

  public generate(): string {
    const probeResult = this.probeBrowserHistory()

    const summary =
      probeResult.entries.length > 0
        ? `Browser activity found ${probeResult.entries.length} recent URL visit(s) from ${probeResult.selectedBrowser} (${probeResult.selectedProfile}).`
        : `Browser activity unavailable: no readable history entries found across ${probeResult.checkedDatabasesCount} detected database(s).`

    const entries =
      probeResult.entries.length > 0
        ? probeResult.entries.slice(0, MAX_OUTPUT_ENTRIES).map((entry, index) => {
            const titleSuffix = entry.title ? ` | title: ${entry.title}` : ''
            return `- ${index + 1}. ${this.formatDateTimeInUserTimezone(entry.visitedAt)} | ${entry.url}${titleSuffix}`
          })
        : ['- No history entries available']

    return [
      `> Recent URLs, browser/profile source, privacy scope. ${summary}`,
      '# BROWSER_HISTORY',
      `- Generated at: ${DateHelper.getDateTime()}`,
      `- Source: ${probeResult.source}`,
      `- Databases checked: ${probeResult.checkedDatabasesCount}`,
      `- Selected browser: ${probeResult.selectedBrowser}`,
      `- Selected profile: ${probeResult.selectedProfile}`,
      '- Privacy scope: full recent URLs.',
      ...entries
    ].join('\n')
  }

  private probeBrowserHistory(): BrowserHistoryProbeResult {
    const databases = this.getBrowserHistoryDatabases()
      .filter((database) => fs.existsSync(database.filePath))
      .slice(0, MAX_DATABASES_TO_QUERY)

    if (databases.length === 0) {
      return {
        source: 'no_supported_browser_history_database',
        checkedDatabasesCount: 0,
        selectedBrowser: 'unknown',
        selectedProfile: 'unknown',
        entries: []
      }
    }

    const latestEntryByDatabase = databases
      .map((database) => {
        const [latestEntry] = this.queryHistoryEntries(database, 1)

        return {
          database,
          latestEntry
        }
      })
      .filter((result) => Boolean(result.latestEntry))

    if (latestEntryByDatabase.length === 0) {
      return {
        source: 'node_sqlite',
        checkedDatabasesCount: databases.length,
        selectedBrowser: 'unknown',
        selectedProfile: 'unknown',
        entries: []
      }
    }

    latestEntryByDatabase.sort((entryA, entryB) => {
      const timestampA = entryA.latestEntry?.visitedAt || ''
      const timestampB = entryB.latestEntry?.visitedAt || ''

      if (timestampA < timestampB) {
        return 1
      }

      if (timestampA > timestampB) {
        return -1
      }

      return 0
    })

    const selectedDatabase = latestEntryByDatabase[0]?.database
    if (!selectedDatabase) {
      return {
        source: 'node_sqlite',
        checkedDatabasesCount: databases.length,
        selectedBrowser: 'unknown',
        selectedProfile: 'unknown',
        entries: []
      }
    }

    const entries = this.queryHistoryEntries(selectedDatabase, MAX_OUTPUT_ENTRIES)

    return {
      source: 'node_sqlite',
      checkedDatabasesCount: databases.length,
      selectedBrowser: selectedDatabase.browser,
      selectedProfile: selectedDatabase.profile,
      entries
    }
  }

  private queryHistoryEntries(
    database: BrowserHistoryDatabase,
    limit: number
  ): BrowserHistoryEntry[] {
    const nodeSqliteScript = `
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
const [dbPath = '', flavor = '', browser = '', profile = '', rawLimit = '64'] = process.argv.slice(1)
const limit = Number(rawLimit) || 64

const normalizeUrl = (rawUrl) => {
  if (typeof rawUrl !== 'string' || rawUrl.length === 0) {
    return ''
  }

  try {
    const parsed = new URL(rawUrl)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return ''
    }

    const normalized = parsed.toString()
    if (normalized.length <= ${MAX_URL_CHARS}) {
      return normalized
    }

    return normalized.slice(0, ${MAX_URL_CHARS} - 3) + '...'
  } catch {
    return ''
  }
}

const normalizeTitle = (title) => {
  if (typeof title !== 'string') {
    return ''
  }

  return title
    .split('\\n')
    .join(' ')
    .split('\\t')
    .join(' ')
    .replace(/  +/g, ' ')
    .trim()
    .slice(0, 56)
}

const toNumber = (value) => {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : NaN
  }

  if (typeof value === 'bigint') {
    return Number(value)
  }

  if (typeof value === 'string') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : NaN
  }

  return NaN
}

const toIso = (timestamp) => {
  const parsedTimestamp = toNumber(timestamp)
  if (!Number.isFinite(parsedTimestamp)) {
    return ''
  }

  return new Date(parsedTimestamp * 1000).toISOString()
}

let db = null
let tempDirectory = ''

try {
  const sqliteModule = await import('better-sqlite3')
  const Database = sqliteModule.default
  if (!Database || !dbPath || !flavor) {
    console.log('[]')
    process.exit(0)
  }

  tempDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'leon-browser-history-'))
  const tempDatabasePath = path.join(tempDirectory, 'history.sqlite')
  fs.copyFileSync(dbPath, tempDatabasePath)
  const walPath = dbPath + '-wal'
  const shmPath = dbPath + '-shm'
  if (fs.existsSync(walPath)) {
    fs.copyFileSync(walPath, tempDatabasePath + '-wal')
  }
  if (fs.existsSync(shmPath)) {
    fs.copyFileSync(shmPath, tempDatabasePath + '-shm')
  }

  db = new Database(tempDatabasePath, {
    readonly: true,
    fileMustExist: true
  })

  let rows = []

  if (flavor === 'chromium') {
    rows = db
      .prepare(
        'SELECT url, title, (last_visit_time / 1000000.0 - 11644473600.0) as ts FROM urls WHERE last_visit_time > 0 ORDER BY last_visit_time DESC LIMIT ?'
      )
      .all(limit)
  } else if (flavor === 'firefox') {
    rows = db
      .prepare(
        'SELECT url, title, (last_visit_date / 1000000.0) as ts FROM moz_places WHERE last_visit_date IS NOT NULL ORDER BY last_visit_date DESC LIMIT ?'
      )
      .all(limit)
  } else if (flavor === 'safari') {
    rows = db
      .prepare(
        'SELECT hi.url as url, hi.title as title, (978307200 + hv.visit_time) as ts FROM history_items hi JOIN history_visits hv ON hv.history_item = hi.id ORDER BY hv.visit_time DESC LIMIT ?'
      )
      .all(limit)
  }

  const entries = rows
    .map((row) => {
      const normalizedUrl = normalizeUrl(row.url)

      if (!normalizedUrl) {
        return null
      }

      return {
        browser,
        profile,
        url: normalizedUrl,
        title: normalizeTitle(row.title),
        visitedAt: toIso(row.ts)
      }
    })
    .filter((entry) => Boolean(entry && entry.visitedAt))

  console.log(JSON.stringify(entries))
} catch {
  console.log('[]')
} finally {
  try {
    if (db) {
      db.close()
    }
  } catch {
    // Ignore close failures.
  }

  try {
    if (tempDirectory && fs.existsSync(tempDirectory)) {
      fs.rmSync(tempDirectory, { recursive: true, force: true })
    }
  } catch {
    // Ignore cleanup failures.
  }
}
    `.trim()

    const output = this.probeHelper.runCommand(process.execPath, [
      '--no-warnings',
      '--input-type=module',
      '-e',
      nodeSqliteScript,
      database.filePath,
      database.flavor,
      database.browser,
      database.profile,
      String(limit)
    ])

    if (!output) {
      return []
    }

    try {
      const parsedEntries = JSON.parse(output) as BrowserHistoryEntry[]

      if (!Array.isArray(parsedEntries)) {
        return []
      }

      return parsedEntries
        .filter((entry) => {
          return (
            !!entry &&
            typeof entry.browser === 'string' &&
            typeof entry.profile === 'string' &&
            typeof entry.url === 'string' &&
            typeof entry.title === 'string' &&
            typeof entry.visitedAt === 'string'
          )
        })
        .sort((entryA, entryB) => {
          if (entryA.visitedAt < entryB.visitedAt) {
            return 1
          }

          if (entryA.visitedAt > entryB.visitedAt) {
            return -1
          }

          return 0
        })
        .slice(0, limit)
    } catch {
      return []
    }
  }

  private getBrowserHistoryDatabases(): BrowserHistoryDatabase[] {
    return [
      ...this.getChromiumHistoryDatabases(),
      ...this.getFirefoxHistoryDatabases(),
      ...this.getSafariHistoryDatabases()
    ]
  }

  private getChromiumHistoryDatabases(): BrowserHistoryDatabase[] {
    const homeDirectory = os.homedir()
    const localAppData =
      process.env['LOCALAPPDATA'] || path.join(homeDirectory, 'AppData', 'Local')

    const browserUserDataRoots = SystemHelper.isWindows()
      ? [
          {
            browser: 'Google Chrome',
            userDataPath: path.join(localAppData, 'Google', 'Chrome', 'User Data')
          },
          {
            browser: 'Microsoft Edge',
            userDataPath: path.join(localAppData, 'Microsoft', 'Edge', 'User Data')
          },
          {
            browser: 'Brave',
            userDataPath: path.join(
              localAppData,
              'BraveSoftware',
              'Brave-Browser',
              'User Data'
            )
          }
        ]
      : SystemHelper.isMacOS()
        ? [
            {
              browser: 'Google Chrome',
              userDataPath: path.join(
                homeDirectory,
                'Library',
                'Application Support',
                'Google',
                'Chrome'
              )
            },
            {
              browser: 'Microsoft Edge',
              userDataPath: path.join(
                homeDirectory,
                'Library',
                'Application Support',
                'Microsoft Edge'
              )
            },
            {
              browser: 'Brave',
              userDataPath: path.join(
                homeDirectory,
                'Library',
                'Application Support',
                'BraveSoftware',
                'Brave-Browser'
              )
            },
            {
              browser: 'Chromium',
              userDataPath: path.join(
                homeDirectory,
                'Library',
                'Application Support',
                'Chromium'
              )
            }
          ]
        : [
            {
              browser: 'Google Chrome',
              userDataPath: path.join(homeDirectory, '.config', 'google-chrome')
            },
            {
              browser: 'Microsoft Edge',
              userDataPath: path.join(homeDirectory, '.config', 'microsoft-edge')
            },
            {
              browser: 'Brave',
              userDataPath: path.join(
                homeDirectory,
                '.config',
                'BraveSoftware',
                'Brave-Browser'
              )
            },
            {
              browser: 'Chromium',
              userDataPath: path.join(homeDirectory, '.config', 'chromium')
            }
          ]

    const databases: BrowserHistoryDatabase[] = []

    for (const root of browserUserDataRoots) {
      if (!fs.existsSync(root.userDataPath)) {
        continue
      }

      let profileDirectories: string[] = []

      try {
        profileDirectories = fs
          .readdirSync(root.userDataPath, { withFileTypes: true })
          .filter((entry) => {
            if (!entry.isDirectory()) {
              return false
            }

            return (
              entry.name === 'Default' ||
              entry.name.startsWith('Profile ') ||
              entry.name.startsWith('Guest Profile')
            )
          })
          .map((entry) => entry.name)
      } catch {
        continue
      }

      for (const profileDirectory of profileDirectories) {
        const historyPath = path.join(root.userDataPath, profileDirectory, 'History')
        databases.push({
          browser: root.browser,
          profile: profileDirectory,
          flavor: 'chromium',
          filePath: historyPath
        })
      }
    }

    return databases
  }

  private getFirefoxHistoryDatabases(): BrowserHistoryDatabase[] {
    const homeDirectory = os.homedir()
    const appData =
      process.env['APPDATA'] || path.join(homeDirectory, 'AppData', 'Roaming')

    const profileRoots = SystemHelper.isWindows()
      ? [path.join(appData, 'Mozilla', 'Firefox', 'Profiles')]
      : SystemHelper.isMacOS()
        ? [
            path.join(
              homeDirectory,
              'Library',
              'Application Support',
              'Firefox',
              'Profiles'
            )
          ]
        : [path.join(homeDirectory, '.mozilla', 'firefox')]

    const databases: BrowserHistoryDatabase[] = []

    for (const profileRoot of profileRoots) {
      if (!fs.existsSync(profileRoot)) {
        continue
      }

      let profileDirectories: string[] = []
      try {
        profileDirectories = fs
          .readdirSync(profileRoot, { withFileTypes: true })
          .filter((entry) => entry.isDirectory())
          .map((entry) => entry.name)
      } catch {
        continue
      }

      for (const profileDirectory of profileDirectories) {
        const historyPath = path.join(profileRoot, profileDirectory, 'places.sqlite')
        databases.push({
          browser: 'Firefox',
          profile: profileDirectory,
          flavor: 'firefox',
          filePath: historyPath
        })
      }
    }

    return databases
  }

  private formatDateTimeInUserTimezone(value: string): string {
    if (!value) {
      return 'unknown'
    }

    return DateHelper.getDateTime(value) || value
  }

  private getSafariHistoryDatabases(): BrowserHistoryDatabase[] {
    if (!SystemHelper.isMacOS()) {
      return []
    }

    const homeDirectory = os.homedir()
    const historyPath = path.join(homeDirectory, 'Library', 'Safari', 'History.db')

    return [
      {
        browser: 'Safari',
        profile: 'Default',
        flavor: 'safari',
        filePath: historyPath
      }
    ]
  }
}
