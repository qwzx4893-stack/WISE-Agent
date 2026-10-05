export type ToolReporter = (input: Record<string, unknown>) => Promise<void>

let toolReporter: ToolReporter | null = null

export const setToolReporter = (reporter: ToolReporter): void => {
  toolReporter = reporter
}

export const reportToolOutput = async (
  input: Record<string, unknown>
): Promise<void> => {
  if (!toolReporter) {
    return
  }

  await toolReporter(input)
}
