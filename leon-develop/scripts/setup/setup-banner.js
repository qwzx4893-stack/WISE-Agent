const SETUP_BANNER = `
██╗     ███████╗ ██████╗ ███╗   ██╗      █████╗ ██╗
██║     ██╔════╝██╔═══██╗████╗  ██║     ██╔══██╗██║
██║     █████╗  ██║   ██║██╔██╗ ██║     ███████║██║
██║     ██╔══╝  ██║   ██║██║╚██╗██║     ██╔══██║██║
███████╗███████╗╚██████╔╝██║ ╚████║     ██║  ██║██║
╚══════╝╚══════╝ ╚═════╝ ╚═╝  ╚═══╝     ╚═╝  ╚═╝╚═╝
`.trim()

const GRADIENT_STOPS = [
  [28, 117, 219],
  [237, 41, 122]
]

function getGradientColor(progress) {
  const scaledProgress = Math.min(
    GRADIENT_STOPS.length - 1,
    Math.max(0, progress) * (GRADIENT_STOPS.length - 1)
  )
  const startIndex = Math.floor(scaledProgress)
  const endIndex = Math.min(GRADIENT_STOPS.length - 1, startIndex + 1)
  const localProgress = scaledProgress - startIndex
  const startColor = GRADIENT_STOPS[startIndex]
  const endColor = GRADIENT_STOPS[endIndex]

  return startColor.map((channel, index) =>
    Math.round(channel + (endColor[index] - channel) * localProgress)
  )
}

function colorizeCharacter(character, progress) {
  if (character === ' ') {
    return character
  }

  const [red, green, blue] = getGradientColor(progress)

  return `\x1b[38;2;${red};${green};${blue}m${character}\x1b[0m`
}

function colorizeBanner(banner) {
  const characters = [...banner]
  const visibleCharacterCount = characters.filter(
    (character) => character !== '\n' && character !== ' '
  ).length
  let visibleIndex = 0

  return characters
    .map((character) => {
      if (character === '\n') {
        return character
      }

      const progress =
        visibleCharacterCount <= 1
          ? 0
          : visibleIndex / (visibleCharacterCount - 1)

      if (character !== ' ') {
        visibleIndex += 1
      }

      return colorizeCharacter(character, progress)
    })
    .join('')
}

/**
 * Print the setup banner once at the beginning of postinstall.
 */
export function printSetupBanner() {
  console.log('')
  console.log(colorizeBanner(SETUP_BANNER))
  console.log('')
}
