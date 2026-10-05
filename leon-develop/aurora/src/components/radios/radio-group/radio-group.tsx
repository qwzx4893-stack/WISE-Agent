import {
  RadioGroup as ArkRadioGroup,
  type RadioGroupRootProps as ArkRadioGroupProps,
  type RadioGroupValueChangeDetails
} from '@ark-ui/react/radio-group'

import './radio-group.sass'

interface RadioGroupOnChangeData {
  name: string
  value: string | number | undefined
}

export interface RadioGroupProps
  extends Pick<
    ArkRadioGroupProps,
    'value' | 'children' | 'defaultValue' | 'disabled'
  > {
  name: string
  onChange?: (data: RadioGroupOnChangeData) => void
}

export function RadioGroup({
  name,
  value,
  children,
  defaultValue,
  disabled,
  onChange
}: RadioGroupProps) {
  return (
    <ArkRadioGroup.Root
      className="aurora-radio-group"
      name={name}
      defaultValue={defaultValue}
      value={value}
      disabled={disabled}
      onValueChange={(event: RadioGroupValueChangeDetails) => {
        const data = {
          name,
          value: event.value
        }

        if (onChange) {
          onChange(data)
        }
      }}
      orientation="horizontal"
    >
      {children}
    </ArkRadioGroup.Root>
  )
}
