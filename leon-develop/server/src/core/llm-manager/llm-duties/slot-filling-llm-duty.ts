import {
  formatParameterDescription,
  LLMDuty,
  type LLMDutyParams,
  type LLMDutyResult
} from '@/core/llm-manager/llm-duty'
import { LLM_MANAGER, LLM_PROVIDER } from '@/core'
import { LLMDuties, SlotFillingStatus } from '@/core/llm-manager/types'
import { LogHelper } from '@/helpers/log-helper'

interface SlotFillingLLMDutyParams {
  input: {
    slotName: string
    slotDescription: string
    slotType: string
    latestUtterance: string
    recentUtterances: string[]
  } | null
  startingUtterance: string
}

const SYSTEM_PROMPT = `You are a highly specialized linguistic model called 'Slot Filling'. Your sole purpose is to extract specific pieces of information ('slots') from a user's latest response.

Rules:
1. Analyze the latest user message in the context of the provided workflow history.
2. Only fill the requested slot.
3. Do not invent or infer information beyond the latest message and provided context.
4. Output a single JSON object:
   - Success: {"filled_slots":{"slot_name":"value"}}
   - Not found: {"status":"${SlotFillingStatus.NotFound}"}
5. Never output anything except JSON.`

export class SlotFillingLLMDuty extends LLMDuty {
  private static instance: SlotFillingLLMDuty
  private readonly startingUtterance: string | null = null
  protected readonly systemPrompt: LLMDutyParams['systemPrompt'] = SYSTEM_PROMPT
  protected readonly name = 'Slot Filling LLM Duty'
  protected input: SlotFillingLLMDutyParams['input'] = null

  constructor(params: SlotFillingLLMDutyParams) {
    super()

    if (!SlotFillingLLMDuty.instance) {
      LogHelper.title(this.name)
      LogHelper.success('New instance')

      SlotFillingLLMDuty.instance = this
    }

    this.input = params.input
    this.startingUtterance = params.startingUtterance
  }

  public async init(): Promise<void> {
    return Promise.resolve()
  }

  public async execute(): Promise<LLMDutyResult | null> {
    LogHelper.title(this.name)
    LogHelper.info('Executing...')

    if (this.input?.slotType && this.input?.slotDescription) {
      this.input.slotDescription = formatParameterDescription({
        type: this.input.slotType,
        description: this.input.slotDescription
      })
    }

    try {
      const prompt = `Workflow context (JSON):
${JSON.stringify(
  {
    startingUtterance: this.startingUtterance,
    recentUtterances: this.input?.recentUtterances || [],
    latestUtterance: this.input?.latestUtterance || ''
  },
  null,
  2
)}

Find the following slot:
- Slot name: "${this.input?.slotName}"
- Slot description: "${this.input?.slotDescription}"`
      const config = LLM_MANAGER.coreLLMDuties[LLMDuties.SlotFilling]
      const completionResult = await LLM_PROVIDER.prompt(prompt, {
        dutyType: LLMDuties.SlotFilling,
        systemPrompt: this.systemPrompt as string,
        temperature: config.temperature,
        maxTokens: config.maxTokens,
        thoughtTokensBudget: config.thoughtTokensBudget,
        disableThinking: true
      })

      if (completionResult?.output) {
        const parsedResult = JSON.parse(completionResult.output)
        const { filled_slots: filledSlots } = parsedResult

        if (filledSlots) {
          ;(completionResult as { output: unknown }).output = {
            status: SlotFillingStatus.Success,
            ...parsedResult
          }
        } else {
          ;(completionResult as { output: unknown }).output = {
            status: SlotFillingStatus.NotFound
          }
        }
      }

      LogHelper.title(this.name)
      LogHelper.success('Duty executed')
      LogHelper.success(`Prompt — ${prompt}`)
      LogHelper.success(`Output — ${JSON.stringify(completionResult?.output)}
usedInputTokens: ${completionResult?.usedInputTokens}
usedOutputTokens: ${completionResult?.usedOutputTokens}`)

      return completionResult as unknown as LLMDutyResult
    } catch (e) {
      LogHelper.title(this.name)
      LogHelper.error(`Failed to execute: ${e}`)
    }

    return null
  }
}
