# AI connections

Buildplate talks to models through [LiteLLM](https://github.com/BerriAI/litellm), so one design agent works with many providers. Each way you reach a model is a **connection**: an API key, a local server, or a hosted OpenAI-compatible service. You can add as many as you like, including several for one provider (for example a work key and a personal key), set one as the default, and choose a different model for any single request from the picker under the Studio prompt.

Connections live in **Settings > AI connections** and take effect immediately.

## Supported connections

| Provider | Method | What you enter | Billing | Where to get it |
| --- | --- | --- | --- | --- |
| Anthropic | Claude API key | API key | Pay per use | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| OpenAI | OpenAI API key | API key | Pay per use | [platform.openai.com](https://platform.openai.com/api-keys) |
| Google Gemini | Gemini API key | API key | Pay per use | [aistudio.google.com](https://aistudio.google.com/apikey) |
| OpenRouter | OpenRouter API key | API key | Pay per use | [openrouter.ai](https://openrouter.ai/keys) |
| xAI | xAI API key | API key | Pay per use | [console.x.ai](https://console.x.ai) |
| Mistral | Mistral API key | API key | Pay per use | [console.mistral.ai](https://console.mistral.ai/api-keys) |
| Groq | Groq API key | API key | Pay per use | [console.groq.com](https://console.groq.com/keys) |
| DeepSeek | DeepSeek API key | API key | Pay per use | [platform.deepseek.com](https://platform.deepseek.com/api_keys) |
| Ollama | Ollama | Base URL | Local | Your Ollama server, for example `http://192.168.1.10:11434` |
| OpenAI-compatible | OpenAI-compatible server | Base URL and optional key | Local | LM Studio, vLLM, LocalAI, llama.cpp server, LiteLLM proxy |

Every request records which connection it used and how it is billed. Consumer chat subscriptions (such as Claude Pro or ChatGPT Plus) cannot be used yet. Support for signing in with a subscription through each vendor's own sanctioned method is planned.

## Adding a connection

Adding or changing connections needs a signed-in web UI password session, because connections hold credentials. Set one in **Settings > Access and MCP** first. The only exception is a browser running on the Buildplate server itself (a direct request to `localhost`, not through a reverse proxy).

1. Under **Add a connection**, choose the provider and method.
2. Give it a name, and paste the key or enter the base URL.
3. Select **Connect**. Buildplate saves the connection (credentials encrypted), then lists the provider's models as a test. If that works, the connection shows **Connected** and its models appear in every model picker. The first working connection becomes the default.

If the test fails, the connection is kept with an **Error** status and the reason. Select **Edit** to fix it, then **Save and test**.

On each connection card:

- **Test** checks the connection again and refreshes the model list.
- **Edit** renames it, replaces the key (leave the key box empty to keep the saved one) or changes the URL. **Remove connection** deletes it and its saved credentials.
- The switch turns the connection off without deleting it. Its models leave the pickers.

## Choosing a model

The design loop asks a lot of a model: it must write correct OpenSCAD, call tools reliably, and ideally read an image of its own render. For the best results:

- **Use a model that supports tool calling.** Models without it still work through a fallback text mode, but they iterate less reliably.
- **Prefer vision-capable models.** With vision, the agent sees a four-view render after each draft and catches geometry mistakes that numbers alone miss.
- **Bigger models make fewer revisions.** A stronger model often finishes in two steps instead of five, which can cost less overall.

Frontier models from Anthropic, OpenAI and Google all handle this well. For local use, recent coder and vision models served by Ollama work for simpler parts.

## Local models with Ollama

1. Run Ollama on your network and pull a model, for example `ollama pull qwen2.5-coder:14b`.
2. Make sure Ollama listens on the network: set `OLLAMA_HOST=0.0.0.0` on the Ollama machine.
3. In Buildplate, add an **Ollama** connection with the base URL. From inside the container, `localhost` means the container itself, so use the host's LAN address, or `http://host.docker.internal:11434` if Ollama runs on the same Docker host.
4. Select **Connect**.

Local requests are recorded with token counts and a cost of $0.

## Image previews

**Settings > Design agent > Image previews** controls whether render previews are sent to the model:

- **Automatic** sends images to models known to support vision.
- **Always send** forces images on, for vision models Buildplate does not recognise.
- **Never send** saves tokens; the agent then relies on the numeric checks only.

**Preview size** sets the resolution. Smaller images cost fewer tokens.

## Extra instructions

Text in **Extra instructions** is added to every request. Use it for house rules such as "always chamfer bottom edges by 0.6 mm", "design for PETG" or "prefer BOSL2 for rounding".
