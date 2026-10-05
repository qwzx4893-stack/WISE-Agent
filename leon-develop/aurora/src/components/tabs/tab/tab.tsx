import {
  Tabs,
  type TabTriggerProps as ArkTabTriggerProps
} from '@ark-ui/react/tabs'

import { generateKeyId } from '../../../lib/utils'

export type TabProps = Pick<
  ArkTabTriggerProps,
  'children' | 'value' | 'disabled'
>

export function Tab({
  children,
  value,
  disabled
}: TabProps): React.JSX.Element {
  return (
    <Tabs.Trigger
      key={`aurora-tab_${generateKeyId()}`}
      className="aurora-tab"
      value={value}
      disabled={disabled}
    >
      {children}
    </Tabs.Trigger>
  )
}
