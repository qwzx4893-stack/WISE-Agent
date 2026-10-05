import { Children, isValidElement, useState, type ReactNode } from 'react'
import { Portal } from '@ark-ui/react/portal'
import {
  Select as ArkSelect,
  createListCollection
} from '@ark-ui/react/select'
import classNames from 'clsx'

import { Flexbox, Icon } from '../../..'
import { type SelectOptionProps } from '../select-option'

import './select.sass'

interface Option {
  label: string
  value: string
  disabled?: boolean
}

interface SelectOnChangeData {
  name: string
  value: string | number | undefined
  label?: string
}

export interface SelectProps {
  name: string
  selectedOption?: Option
  defaultValue?: string
  disabled?: boolean
  placeholder: string
  children: ReactNode
  onChange?: (data: SelectOnChangeData) => void
}

export function Select({
  name,
  placeholder,
  children,
  selectedOption,
  defaultValue,
  disabled,
  onChange
}: SelectProps) {
  const [currentValue, setCurrentValue] = useState(
    selectedOption?.value ?? defaultValue
  )
  const options = Children.toArray(children).flatMap((child) => {
    if (!isValidElement<SelectOptionProps>(child)) {
      return []
    }

    const { label, value, disabled } = child.props

    return [
      {
        label,
        value,
        disabled
      }
    ]
  })
  const collection = createListCollection<Option>({
    items: options,
    itemToString: (item) => item.label,
    itemToValue: (item) => item.value,
    isItemDisabled: (item) => !!item.disabled
  })
  const selectedValue = selectedOption?.value
  const hasSelectedValue = Boolean(selectedValue ?? currentValue)

  return (
    <ArkSelect.Root
      collection={collection}
      closeOnSelect
      value={selectedValue ? [selectedValue] : undefined}
      defaultValue={defaultValue ? [defaultValue] : undefined}
      disabled={disabled}
      name={name}
      onValueChange={(event) => {
        const nextSelectedOption = event.items[0]

        setCurrentValue(nextSelectedOption?.value)

        const data = {
          name,
          label: nextSelectedOption?.label,
          value: nextSelectedOption?.value
        }

        if (onChange) {
          onChange(data)
        }
      }}
    >
      <ArkSelect.HiddenSelect />
      <ArkSelect.Trigger
        className={classNames('aurora-select-trigger', {
          'aurora-select-trigger--selected': hasSelectedValue
        })}
      >
        <Flexbox
          flexDirection="row"
          alignItems="center"
          justifyContent="space-between"
        >
          <div className="aurora-select-trigger-placeholder-container">
            <ArkSelect.ValueText placeholder={placeholder} />
          </div>
          <div className="aurora-select-trigger-icon-container">
            <Icon iconName="arrow-down-s" />
          </div>
        </Flexbox>
      </ArkSelect.Trigger>
      <Portal>
        <ArkSelect.Positioner>
          <ArkSelect.Content className="aurora-select-content">
            {children}
          </ArkSelect.Content>
        </ArkSelect.Positioner>
      </Portal>
    </ArkSelect.Root>
  )
}
