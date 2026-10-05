import { useState } from 'react'
import {
  Switch as ArkSwitch,
  type SwitchCheckedChangeDetails,
  type SwitchRootProps as ArkSwitchProps
} from '@ark-ui/react/switch'

import './switch.sass'

interface SwitchOnChangeData {
  name: string
  value: string | number | undefined
  isSwitched: boolean
}

export interface SwitchProps
  extends Pick<ArkSwitchProps, 'value' | 'checked' | 'disabled' | 'required'> {
  name: string
  label?: string
  onChange?: (data: SwitchOnChangeData) => void
}

export function Switch({
  name,
  label,
  checked,
  value,
  disabled,
  required,
  onChange
}: SwitchProps) {
  const [isChecked, setIsChecked] = useState(checked)

  return (
    <ArkSwitch.Root
      className="aurora-switch"
      name={name}
      value={value}
      checked={isChecked}
      disabled={disabled}
      required={required}
      onCheckedChange={(event: SwitchCheckedChangeDetails) => {
        setIsChecked(event.checked)

        const data = {
          name,
          value,
          isSwitched: event.checked
        }

        if (onChange) {
          onChange(data)
        }
      }}
    >
      <ArkSwitch.HiddenInput />
      <ArkSwitch.Control className="aurora-switch-control">
        <ArkSwitch.Thumb className="aurora-switch-thumb" />
      </ArkSwitch.Control>
      <ArkSwitch.Label className="aurora-switch-label">{label}</ArkSwitch.Label>
    </ArkSwitch.Root>
  )
}
