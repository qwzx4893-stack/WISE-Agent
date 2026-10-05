import {
  Tabs,
  type TabListProps as ArkTabListProps
} from '@ark-ui/react/tabs'

import { generateKeyId } from '../../../lib/utils'

export type TabListProps = Pick<ArkTabListProps, 'children'>

export function TabList({ children }: TabListProps): React.JSX.Element {
  return (
    <Tabs.List
      key={`aurora-tab-list_${generateKeyId()}`}
      className="aurora-tab-list"
    >
      {children}
      <Tabs.Indicator className="aurora-tab-indicator-container">
        <div className="aurora-tab-indicator" />
      </Tabs.Indicator>
    </Tabs.List>
  )
}
