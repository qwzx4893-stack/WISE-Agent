import { useState } from 'react'
import classNames from 'clsx'
import {
  Slider,
  type SliderRootProps as ArkSliderProps,
  type SliderValueChangeDetails
} from '@ark-ui/react/slider'

import './range-slider.sass'

interface RangeSliderOnChangeData {
  name: string
  value: string | number | undefined
}

export interface RangeSliderProps
  extends Pick<
    ArkSliderProps,
    | 'max'
    | 'min'
    | 'step'
    | 'disabled'
    | 'orientation'
  > {
  name: string
  value?: number | number[]
  defaultValue?: number | number[]
  width?: number | string
  height?: number | string
  hiddenThumb?: boolean
  onChange?: (data: RangeSliderOnChangeData) => void
}

function normalizeValue(value?: number | number[]): number[] | undefined {
  if (typeof value === 'undefined') {
    return undefined
  }

  return Array.isArray(value) ? value : [value]
}

export function RangeSlider({
  name,
  width,
  height,
  value,
  defaultValue,
  max = 100,
  min = 0,
  step = 1,
  disabled,
  orientation = 'horizontal',
  hiddenThumb,
  onChange
}: RangeSliderProps) {
  const [newValue, setNewValue] = useState(normalizeValue(value ?? defaultValue))
  const normalizedValue = normalizeValue(value)
  const normalizedDefaultValue = normalizeValue(defaultValue)
  const currentValue = normalizedValue ?? newValue ?? normalizedDefaultValue
  const currentScalarValue = currentValue?.[0] ?? min
  const valueInPercent =
    Number((((currentScalarValue - min) / (max - min)) * 100).toFixed(2))

  return (
    <div
      className="aurora-range-slider-container"
      style={{
        width,
        height
      }}
    >
      <Slider.Root
        className={classNames('aurora-range-slider', {
          'aurora-range-slider--hidden-thumb': hiddenThumb
        })}
        name={name}
        value={normalizedValue}
        defaultValue={normalizedDefaultValue}
        max={max}
        min={min}
        step={step}
        disabled={disabled}
        orientation={orientation}
        onValueChange={(event: SliderValueChangeDetails) => {
          setNewValue(event.value)

          const data = {
            name,
            value: event.value[0]
          }

          if (onChange) {
            onChange(data)
          }
        }}
      >
        <Slider.Control className="aurora-range-slider-control">
          <Slider.Track className="aurora-range-slider-track">
            <Slider.Range
              className="aurora-range-slider-range"
              style={{
                [orientation === 'horizontal' ? 'width' : 'height']:
                  `${valueInPercent}%`
              }}
            />
          </Slider.Track>
          <Slider.Thumb className="aurora-range-slider-thumb" index={0}>
            <Slider.HiddenInput />
          </Slider.Thumb>
        </Slider.Control>
      </Slider.Root>
    </div>
  )
}
