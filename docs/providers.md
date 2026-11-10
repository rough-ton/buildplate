# AI connections

Buildplate talks to models through [LiteLLM](https://github.com/BerriAI/litellm), so one design agent works with many providers. Each way you reach a model is a **connection**: an API key, a local server, or a hosted OpenAI-compatible service. You can add as many as you like, including several for one provider (for example a work key and a personal key), set one as the default, and choose a different model for any single request from the picker under the Studio prompt.

Connections live in **Settings > AI connections** and take effect immediately.

## Supported connections

| Provider | Method | What you enter | Billing | Where to get it |
| --- | --- | --- | --- | --- |
| Anthropic | Claude API key | API key | Pay per use | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| Anthropic | Claude plan (Claude Code) | Nothing: you sign in to Claude | Uses your plan | A Claude Pro, Max, Team or Enterprise plan. See [below](#claude-plan) |
| OpenAI | OpenAI API key | API key | Pay per use | [platform.openai.com](https://platform.openai.com/api-keys) |
| Google Gemini | Gemini API key | API key | Pay per use | [aistudio.google.com](https://aistudio.google.com/apikey) |
| OpenRouter | OpenRouter API key | API key | Pay per use | [openrouter.ai](https://openrouter.ai/keys) |
| xAI | xAI API key | API key | Pay per use | [console.x.ai](https://console.x.ai) |
| Mistral | Mistral API key | API key | Pay per use | [console.mistral.ai](https://console.mistral.ai/api-keys) |
| Groq | Groq API key | API key | Pay per use | [console.groq.com](https://console.groq.com/keys) |
| DeepSeek | DeepSeek API key | API key | Pay per use | [platform.deepseek.com](https://platform.deepseek.com/api_keys) |
| Ollama | Ollama | Base URL | Local | Your Ollama server, for example `http://192.168.1.10:11434` |
| OpenAI-compatible | OpenAI-compatible server | Base URL and optional key | Local | LM Studio, vLLM, LocalAI, llama.cpp server, LiteLLM proxy |

Every request records which connection it used and how it is billed. A Claude plan can be used through Claude Code, as described below. Other consumer subscriptions, such as ChatGPT Plus, cannot be used yet.

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

## Claude plan

The **Claude plan (Claude Code)** connection uses your Claude Pro, Max, Team or Enterprise plan instead of an API key. Buildplate runs [Claude Code](https://code.claude.com), Anthropic's own command-line app, unmodified, and you sign in to it with your Claude account through Claude Code's own login.

**Before you start:** set a web UI password and sign in. Claude plan connections always need one, even from the server itself.

1. Under **Add a connection**, choose **Anthropic > Claude plan (Claude Code)**, name it, and select **Add and sign in**.
2. The first time, Buildplate downloads Claude Code (about 280 MB) from Anthropic into the data folder and checks it against Anthropic's published checksum. This needs internet access from the container.
3. A terminal opens running Claude Code's sign-in. Open the link it shows, sign in to your Claude account and approve access, then copy the code the page shows and paste it into the terminal (or the box under it) and press Enter.
4. When it says the sign-in worked, close the terminal. The card shows **Connected** and the plan type.

Pick `sonnet`, `opus`, `haiku` or `fable` from the model picker. Claude Code resolves each to the newest model of that tier; Usage records the exact model. You can also type a full model ID.

How it works and what it can do:

- Claude Code keeps your sign-in in its own files inside the data folder (`/data/claude-code/<connection>`). Buildplate never reads, copies or stores your Claude credentials; it asks Claude Code whether it is signed in.
- Each Claude plan connection has its own sign-in, so you can add a work plan and a personal plan side by side.
- For each request, Buildplate runs Claude Code with all of its built-in tools turned off (no shell, file or web access) in an empty folder. Claude Code can only call Buildplate's `check_design` and `submit_design`, through a private endpoint that only accepts that request's one-time token.
- Requests use your plan's normal usage limits. Studio and Usage show **Included in plan** instead of a cost, with the API-price equivalent on hover. When the plan's limit is reached, the request stops with a message saying so.
- **Sign out** on the card signs Claude Code out. Removing the connection signs out and deletes its Claude Code folder.
- Buildplate does not resell or pay for Claude usage. You use your own plan under your own agreement with Anthropic.

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
