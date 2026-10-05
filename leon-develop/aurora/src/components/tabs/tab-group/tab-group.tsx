import classNames from 'clsx'
import {
  Tabs,
  type TabsRootProps as ArkTabsProps,
  type TabsValueChangeDetails
} from '@ark-ui/react/tabs'

import { generateKeyId } from '../../../lib/utils'

import './tab-group.sass'

export interface TabGroupProps
  extends Pick<ArkTabsProps, 'children' | 'defaultValue'> {
  size?: 'sm' | 'md' | 'lg'
  onChange?: (details: TabsValueChangeDetails) => void
}

export function TabGroup({
  children,
  defaultValue,
  onChange,
  size
}: TabGroupProps) {
  return (
    <Tabs.Root
      key={`aurora-tab-group_${generateKeyId()}`}
      className={classNames('aurora-tab-group', {
        [`aurora-tab-group--${size}`]: size
      })}
      defaultValue={defaultValue}
      onValueChange={onChange}
      orientation="horizontal"
    >
      {children}
    </Tabs.Root>
  )
}
