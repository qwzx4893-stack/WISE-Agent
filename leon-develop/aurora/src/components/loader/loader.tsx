import { generateKeyId } from '../../lib/utils'

import './loader.sass'

// interface Props {
// size?: 'sm' | 'md'
// }

export type LoaderProps = Record<string, never>

export function Loader(): React.JSX.Element {
  return (
    <span className="aurora-loader" key={`aurora-loader_${generateKeyId()}`} />
    /*<span
      className={classNames('aurora-loader', {
        [`aurora-loader--${size}`]: size
      })}
    />*/
  )
}
