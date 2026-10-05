import {
  Select as ArkSelect
} from '@ark-ui/react/select'

export interface SelectOptionProps {
  disabled?: boolean
  label: string
  value: string
}

export function SelectOption({
  label,
  value,
  disabled = false
}: SelectOptionProps) {
  return (
    <ArkSelect.Item
      className="aurora-select-option"
      item={{
        label,
        value,
        disabled
      }}
    >
      <ArkSelect.ItemText>{label}</ArkSelect.ItemText>
    </ArkSelect.Item>
  )
}
