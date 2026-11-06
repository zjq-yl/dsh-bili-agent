type ModelConfig = { provider: string; model: string; timeoutMs?: number }

export type ModelSelection = {
  provider: string
  model: string
  source: 'plugin' | 'host_default' | 'missing' | 'incomplete_plugin_config'
  configured: boolean
}

/** An explicit pair wins; otherwise inherit the host's current default pair. */
export function resolveModelConfig(
  config: Pick<ModelConfig, 'provider' | 'model'>,
  hostSelection?: Pick<ModelConfig, 'provider' | 'model'>,
): ModelSelection {
  if (config.provider || config.model) {
    const configured = !!(config.provider && config.model)
    return { provider: config.provider, model: config.model, configured,
      source: configured ? 'plugin' : 'incomplete_plugin_config' }
  }
  if (hostSelection?.provider && hostSelection.model) {
    return { provider: hostSelection.provider, model: hostSelection.model,
      source: 'host_default', configured: true }
  }
  return { provider: '', model: '', source: 'missing', configured: false }
}

// 只消费宿主公开的流接口；模型凭据、工具权限不进入 Python。
export async function generateText(llm: any, config: ModelConfig, params: any, signal: AbortSignal) {
  if (!config.provider || !config.model) throw new Error('MODEL_NOT_CONFIGURED')
  if (!params || typeof params.system !== 'string' || typeof params.text !== 'string'
      || params.system.length + params.text.length > 48000) throw new Error('INVALID_MODEL_INPUT')
  const controller = new AbortController()
  const combined = AbortSignal.any([signal, controller.signal])
  const timer = setTimeout(() => controller.abort(), config.timeoutMs ?? 60000)
  let text = ''
  let usage: Record<string, number> | null = null
  let finish = ''
  try {
    combined.throwIfAborted()
    for await (const chunk of llm.stream({
      provider: config.provider, model: config.model, system: params.system,
      messages: [{ role: 'user', content: [{ type: 'text', text: params.text }] }],
      maxTokens: params.purpose === 'dm_reply' ? 1200 : 4096, signal: combined,
    })) {
      combined.throwIfAborted()
      if (chunk.type === 'text-delta') {
        text += chunk.text
        if (text.length > 32000) throw new Error('MODEL_OUTPUT_TOO_LARGE')
      } else if (chunk.type === 'usage') {
        usage = {}
        for (const key of ['inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens']) {
          const value = chunk.usage[key]
          if (Number.isSafeInteger(value) && value >= 0) usage[key] = value
        }
      } else if (chunk.type === 'finish') {
        finish = chunk.reason.kind
        break
      }
    }
    if (finish !== 'stop' || !text.trim()) throw new Error('MODEL_INCOMPLETE')
    return { text, usage, provider: config.provider, model: config.model }
  } finally { clearTimeout(timer); controller.abort() }
}
