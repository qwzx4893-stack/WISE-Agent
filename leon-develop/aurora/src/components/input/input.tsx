import { useEffect, useState } from 'react'
import classNames from 'clsx'

import { Text, Icon } from '../..'
import type { IconProps } from '../icon'

import './input.sass'

export interface InputProps {
  name: string
  placeholder: string
  required?: boolean
  value?: string
  type?:
    | 'text'
    | 'password'
    | 'email'
    | 'tel'
    | 'url'
    | 'number'
    | 'date'
    | 'time'
    | 'datetime-local'
    | 'month'
    | 'week'
    | 'color'
  iconName?: string
  iconSVG?: IconProps['svg']
  iconType?: IconProps['type']
  iconSize?: IconProps['size']
  hint?: string
  disabled?: boolean
  height?: number | 'auto'
  minLength?: number
  maxLength?: number
  min?: number
  max?: number
  step?: number
  pattern?: string
  multiline?: boolean
  autofocus?: boolean
  onFocus?: () => void
  onBlur?: () => void
  onKeyDown?: (event: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) => void
  inputRef?: React.Ref<HTMLInputElement | HTMLTextAreaElement>
  onChange?: (value: string) => void
}

export function Input({
  name,
  placeholder,
  required = false,
  type = 'text',
  iconName,
  iconSVG,
  iconType = 'fill',
  iconSize = 'md',
  hint,
  value,
  disabled,
  height = 'auto',
  minLength,
  maxLength,
  pattern,
  multiline,
  autofocus,
  onFocus,
  onBlur,
  onKeyDown,
  inputRef,
  onChange
}: InputProps) {
  const [inputValue, setInputValue] = useState(value || '')

  useEffect(() => {
    setInputValue(value || '')
  }, [value])

  if (!multiline) {
    if (!maxLength) {
      maxLength = 64
    }

    if (height !== 'auto') {
      height = 'auto'
    }
  }

  return (
    <div className="aurora-input-container">
      {multiline ? (
        <textarea
          name={name}
          placeholder={placeholder}
          required={required}
          value={inputValue}
          disabled={disabled}
          autoFocus={autofocus}
          minLength={minLength}
          maxLength={maxLength}
          onFocus={onFocus}
          onBlur={onBlur}
          onKeyDown={onKeyDown}
          ref={inputRef as React.Ref<HTMLTextAreaElement>}
          onChange={(e) => {
            setInputValue(e.target.value)

            if (onChange) {
              onChange(e.target.value)
            }
          }}
          style={{ height }}
          className={classNames('aurora-input', {
            'aurora-input--multiline': true,
            'aurora-input--disabled': disabled,
            'aurora-input--with-icon': !!iconName || !!iconSVG
          })}
        />
      ) : (
        <input
          type={type}
          name={name}
          placeholder={placeholder}
          required={required}
          value={inputValue}
          disabled={disabled}
          autoFocus={autofocus}
          minLength={minLength}
          maxLength={maxLength}
          pattern={pattern}
          onFocus={onFocus}
          onBlur={onBlur}
          onKeyDown={onKeyDown}
          ref={inputRef as React.Ref<HTMLInputElement>}
          onChange={(e) => {
            setInputValue(e.target.value)

            if (onChange) {
              onChange(e.target.value)
            }
          }}
          className={classNames('aurora-input', {
            'aurora-input--disabled': disabled,
            'aurora-input--with-icon': !!iconName || !!iconSVG
          })}
        />
      )}
      {(iconName || iconSVG) && (
        <div className="aurora-input-icon-container">
          <Icon
            iconName={iconName}
            svg={iconSVG}
            type={iconType}
            size={iconSize}
          />
        </div>
      )}
      {hint && (
        <div className="aurora-input-hint-container">
          <Text fontSize="xs" tertiary>
            {hint}
          </Text>
        </div>
      )}
    </div>
  )
}
