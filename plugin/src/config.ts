import Schema from '@deepseek-ai/schemastery'

export const Config = Schema.object({
  pythonPath: Schema.string().default('python'),
  dataDir: Schema.string().default(''),
  provider: Schema.string().default('').description('与 model 同时留空时继承 DSH 当前默认模型；显式配置时需同时填写。'),
  model: Schema.string().default('').description('模型 ID；与 provider 同时留空时继承宿主默认选择。'),
  asrModelPath: Schema.string().default(''),
  dmSendEnabled: Schema.boolean().default(false),
  dmAllowedPeers: Schema.string().default(''),
  enablePlayer: Schema.boolean().default(false),
  enablePanel: Schema.boolean().default(true).description('启动本地控制面板，与当前插件共用工作进程。'),
  panelPort: Schema.natural().max(65535).default(0).description('面板端口，0表示自动选择空闲端口。'),
})

export type PluginConfig = {
  pythonPath: string
  dataDir: string
  provider: string
  model: string
  asrModelPath: string
  dmSendEnabled: boolean
  dmAllowedPeers: string
  enablePlayer: boolean
  enablePanel: boolean
  panelPort: number
}
