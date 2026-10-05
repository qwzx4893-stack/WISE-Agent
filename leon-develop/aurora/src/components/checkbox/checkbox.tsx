import { useState } from 'react'
import {
  Checkbox as ArkCheckbox,
  type CheckboxCheckedChangeDetails,
  type CheckboxRootProps as ArkCheckboxProps
} from '@ark-ui/react/checkbox'

import { Icon } from '../icon'

import './checkbox.sass'

interface CheckboxOnChangeData {
  name: string
  value: string | undefined
  isChecked: boolean
}

export interface CheckboxProps
  extends Pick<
    ArkCheckboxProps,
    'value' | 'checked' | 'disabled' | 'required'
  > {
  name: string
  label?: string
  onChange?: (data: CheckboxOnChangeData) => void
}

export function Checkbox({
  name,
  label,
  checked,
  value,
  disabled,
  required,
  onChange
}: CheckboxProps) {
  const [isChecked, setIsChecked] = useState(checked)

  return (
    <ArkCheckbox.Root
      className="aurora-checkbox"
      name={name}
      value={value}
      checked={isChecked}
      disabled={disabled}
      required={required}
      onCheckedChange={(event: CheckboxCheckedChangeDetails) => {
        setIsChecked(event.checked as boolean)

        const data = {
          name,
          value,
          isChecked: !!event.checked
        }

        if (onChange) {
          onChange(data)
        }
      }}
    >
      <ArkCheckbox.HiddenInput />
      <ArkCheckbox.Control className="aurora-checkbox-control">
        {isChecked ? (
          <Icon iconName="check" size="sm" animated />
        ) : (
          <div className="aurora-checkbox-placeholder" />
        )}
      </ArkCheckbox.Control>
      <ArkCheckbox.Label className="aurora-checkbox-label">
        {label}
      </ArkCheckbox.Label>
    </ArkCheckbox.Root>
  )
}
