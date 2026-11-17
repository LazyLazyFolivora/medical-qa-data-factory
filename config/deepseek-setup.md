# DeepSeek 官方 API 配置

配置已加入 `models.json` 和 `models.example.json` 的 `deepseek-official`。DeepSeek 配置保留；当前默认 provider 为 qwen-self-hosted，Qwen 上下文窗口为 131072 tokens。

## 填写密钥

在项目根目录 `.env.deepseek` 中填写真实 DeepSeek API key，只写密钥本身，不写变量名或引号。该文件已被 `.gitignore` 的 `.env.*` 规则排除。

在项目根目录打开 PowerShell，启动后端前运行下面一行，把文件内容加载到当前终端环境变量：

```powershell
$env:DEEPSEEK_API_KEY = (Get-Content -LiteralPath .env.deepseek -Raw).Trim()
```

然后在同一终端使用原来的命令启动后端。已启动的后端需要重启。
程序不会自动读取此文件；以上命令负责加载。密钥不要写入 models.json 或提交到 Git。

## 选择模型

单个任务选择 provider `deepseek-official`（请求字段 `model_provider_id`）。
希望所有未指定模型的新任务使用 DeepSeek 时，将 models.json 顶层 default_provider 改为 deepseek-official。
切回时改回 qwen-self-hosted。原有任务的历史不会被改写。

## 参数选择

- 官方 Chat Completions 地址：https://api.deepseek.com/chat/completions
- 模型：deepseek-flash。
- JSON 输出：json_object；不继承 Qwen 的主 agent json_schema 配置。
- reasoning_effort：none，明确关闭思考，便于与现有非思考模式做初步对照。
- DeepSeek 本地上下文窗口为 1,000,000 tokens（1M），输出预留 4096，安全余量 1024；可用输入预算为 994,880 tokens。Qwen 的上下文窗口独立配置，当前为 131072 tokens。
- tokenization_url：null，不使用 Qwen 分词器；本地估算不等于服务端准确 token 数。
- 使用 DeepSeek 官方账户的密钥和余额，ChatGPT Pro 额度不适用于 DeepSeek。

尚未提供密钥，未执行付费请求或真实连通性测试。

官方文档（2026-11-10 核对）：
- https://api-docs.deepseek.com/quick_start/pricing/
- https://api-docs.deepseek.com/api/create-chat-completion/
