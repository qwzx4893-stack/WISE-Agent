export abstract class ContextFile {
  public abstract readonly filename: string
  public abstract readonly ttlMs: number | null

  public abstract generate(): string
}
