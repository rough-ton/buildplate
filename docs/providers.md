# AI providers

Buildplate talks to models through [LiteLLM](https://github.com/BerriAI/litellm), so one design agent works with many providers. You can connect several at once, set a default, and choose a different model for any single request from the picker under the Studio prompt.

All provider settings live in **Settings > AI providers** and take effect immediately.

## Supported providers

| Provider | What you enter | Where to get it |
| --- | --- | --- |
| Anthropic (Claude) | API key | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| OpenAI | API key | [platform.openai.com](https://platform.openai.com/api-keys) |
| Google Gemini | API key | [aistudio.google.com](https://aistudio.google.com/apikey) |
| OpenRouter | API key | [openrouter.ai](https://openrouter.ai/keys) |
| Ollama | Base URL | Your Ollama server, for example `http://192.168.1.10:11434` |
| OpenAI-compatible | Base URL and optional key | LM Studio, vLLM, LocalAI, llama.cpp server, LiteLLM proxy |

Buildplate uses API keys. Consumer chat subscriptions (such as Claude Pro or ChatGPT Plus) are separate products and cannot be used here.

## Connecting a provider

1. Paste the key, or enter the base URL for a local server.
2. Select **Connect** (or **Test and load models** once a key is saved).
3. Buildplate calls the provider's model list. If that works, the key is saved encrypted, the provider is turned on, and its models appear in every model picker.

To replace a key, paste the new one and test again. To remove it, select **Remove key**.

## Choosing a model

The design loop asks a lot of a model: it must write correct OpenSCAD, call tools reliably, and ideally read an image of its own render. For the best results:

- **Use a model that supports tool calling.** Models without it still work through a fallback text mode, but they iterate less reliably.
- **Prefer vision-capable models.** With vision, the agent sees a four-view render after each draft and catches geometry mistakes that numbers alone miss.
- **Bigger models make fewer revisions.** A stronger model often finishes in two steps instead of five, which can cost less overall.

Frontier models from Anthropic, OpenAI and Google all handle this well. For local use, recent coder and vision models served by Ollama work for simpler parts.

## Local models with Ollama

1. Run Ollama on your network and pull a model, for example `ollama pull qwen2.5-coder:14b`.
2. Make sure Ollama listens on the network: set `OLLAMA_HOST=0.0.0.0` on the Ollama machine.
3. In Buildplate, enter the base URL. From inside the container, `localhost` means the container itself, so use the host's LAN address, or `http://host.docker.internal:11434` if Ollama runs on the same Docker host.
4. Select **Test and load models**.

Local requests are recorded with token counts and a cost of $0.

## Image previews

**Settings > Design agent > Image previews** controls whether render previews are sent to the model:

- **Automatic** sends images to models known to support vision.
- **Always send** forces images on, for vision models Buildplate does not recognise.
- **Never send** saves tokens; the agent then relies on the numeric checks only.

**Preview size** sets the resolution. Smaller images cost fewer tokens.

## Extra instructions

Text in **Extra instructions** is added to every request. Use it for house rules such as "always chamfer bottom edges by 0.6 mm", "design for PETG" or "prefer BOSL2 for rounding".
