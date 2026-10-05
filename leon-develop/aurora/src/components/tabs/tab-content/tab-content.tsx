import {
  Tabs,
  type TabContentProps as ArkTabContentProps
} from '@ark-ui/react/tabs'

import { generateKeyId } from '../../../lib/utils'

export type TabContentProps = Pick<ArkTabContentProps, 'children' | 'value'>

export function TabContent({
  children,
  value
}: TabContentProps): React.JSX.Element {
  return (
    <Tabs.Content
      key={`aurora-tab-content_${generateKeyId()}`}
      className="aurora-tab-content"
      value={value}
    >
      {children}
    </Tabs.Content>
  )
}
