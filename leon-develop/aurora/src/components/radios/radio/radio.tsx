import {
  RadioGroup,
  type RadioGroupItemProps as ArkRadioProps
} from '@ark-ui/react/radio-group'

export interface RadioProps extends Pick<ArkRadioProps, 'value' | 'disabled'> {
  label: string
}

export function Radio({ label, value, disabled }: RadioProps) {
  return (
    <RadioGroup.Item
      className="aurora-radio"
      value={value}
      disabled={disabled}
    >
      <RadioGroup.ItemHiddenInput />
      <RadioGroup.ItemControl className="aurora-radio-control" />
      <RadioGroup.ItemText className="aurora-radio-label">
        {label}
      </RadioGroup.ItemText>
    </RadioGroup.Item>
  )
}
