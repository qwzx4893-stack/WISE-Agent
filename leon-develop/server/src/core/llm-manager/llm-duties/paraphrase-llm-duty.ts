import {
  LLMDuty,
  type LLMDutyExecuteParams,
  type LLMDutyInitParams,
  type LLMDutyParams,
  type LLMDutyResult
} from '@/core/llm-manager/llm-duty'
import { EVENT_EMITTER, LLM_MANAGER, LLM_PROVIDER, PERSONA } from '@/core'
import { LLMDuties } from '@/core/llm-manager/types'
import { LogHelper } from '@/helpers/log-helper'

type ParaphraseLLMDutyParams = LLMDutyParams

export class ParaphraseLLMDuty extends LLMDuty {
  private static instance: ParaphraseLLMDuty
  protected static finalSystemPrompt = ''
  protected systemPrompt = `You are an AI system that generates answers (Natural Language Generation).
You must provide a text alternative according to your current mood and your personality.
Never indicate that it's a modified version.
Do not interpret the text, just paraphrase it.
You do not ask question if the original text does not contain any.
If there are data in the original text, make sure to provide them.

Examples:

Modify this text: I added your items to the shopping list.
I included the items you mentioned to the shopping list. Happy shopping!

Modify this text: the sun is a star.
The sun is a star, it is the closest star to Earth.`
  protected readonly name = 'Paraphrase LLM Duty'
  protected input: LLMDutyParams['input'] = null

  constructor(params: ParaphraseLLMDutyParams) {
    super()

    if (!ParaphraseLLMDuty.instance) {
      LogHelper.title(this.name)
      LogHelper.success('New instance')

      ParaphraseLLMDuty.instance = this

      EVENT_EMITTER.on('persona_new-mood-set', async () => {
        await this.init({ force: true })
      })
      EVENT_EMITTER.on('persona_new-info-set', async () => {
        await this.init({ force: true })
      })
    }

    this.input = params.input
  }

  public async init(params?: LLMDutyInitParams): Promise<void> {
    void params

    ParaphraseLLMDuty.finalSystemPrompt = PERSONA.getDutySystemPrompt(
      this.systemPrompt
    )
  }

  public async execute(
    params?: LLMDutyExecuteParams
  ): Promise<LLMDutyResult | null> {
    void params

    LogHelper.title(this.name)
    LogHelper.info('Executing...')

    try {
      if (!ParaphraseLLMDuty.finalSystemPrompt) {
        await this.init()
      }

      const prompt = `Modify the following text but do not say you modified it: ${this.input}`
      const config = LLM_MANAGER.coreLLMDuties[LLMDuties.Paraphrase]
      let completionResult = await LLM_PROVIDER.prompt(prompt, {
        dutyType: LLMDuties.Paraphrase,
        systemPrompt: ParaphraseLLMDuty.finalSystemPrompt,
        maxTokens: config?.maxTokens,
        temperature: config?.temperature,
        thoughtTokensBudget: config?.thoughtTokensBudget,
        disableThinking: true
      })

      if (!completionResult) {
        const providerError = LLM_PROVIDER.consumeLastProviderErrorMessage()
        if (!providerError) {
          return null
        }

        completionResult = {
          dutyType: LLMDuties.Paraphrase,
          systemPrompt: ParaphraseLLMDuty.finalSystemPrompt,
          input: prompt,
          output: providerError,
          data: null,
          maxTokens: 0,
          thoughtTokensBudget: 0,
          usedInputTokens: 0,
          usedOutputTokens: 0,
          generationDurationMs: 0,
          temperature: config?.temperature ?? 0
        }
      }

      LogHelper.title(this.name)
      LogHelper.success('Duty executed')
      LogHelper.success(`Prompt — ${prompt}`)
      LogHelper.success(`Output — ${completionResult?.output}
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
