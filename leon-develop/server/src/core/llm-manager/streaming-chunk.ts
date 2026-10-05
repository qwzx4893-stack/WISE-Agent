const MIN_STREAMING_OVERLAP_DEDUP_LENGTH = 2

export function mergeStreamingChunk(
  accumulated: string,
  incoming: string
): string {
  if (!incoming) {
    return ''
  }

  if (!accumulated) {
    return incoming
  }

  // Preserve single-character boundaries because they can be legitimate
  // repeated letters split across chunks (for example "LLM" as "L" + "LM").
  if (
    incoming.length >= MIN_STREAMING_OVERLAP_DEDUP_LENGTH &&
    accumulated.endsWith(incoming)
  ) {
    return ''
  }

  if (
    accumulated.length >= MIN_STREAMING_OVERLAP_DEDUP_LENGTH &&
    incoming.startsWith(accumulated)
  ) {
    return incoming.slice(accumulated.length)
  }

  // If this is a long repeated chunk already present, skip it.
  if (incoming.length >= 32 && accumulated.includes(incoming)) {
    return ''
  }

  // Keep only the non-overlapping suffix of incoming.
  const maxOverlap = Math.min(accumulated.length, incoming.length)
  for (
    let overlap = maxOverlap;
    overlap >= MIN_STREAMING_OVERLAP_DEDUP_LENGTH;
    overlap -= 1
  ) {
    if (accumulated.slice(-overlap) === incoming.slice(0, overlap)) {
      return incoming.slice(overlap)
    }
  }

  return incoming
}
