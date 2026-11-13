# AI connections

Buildplate talks to models through [LiteLLM](https://github.com/BerriAI/litellm), so one design agent works with many providers. Each way you reach a model is a **connection**: an API key, a local server, or a hosted OpenAI-compatible service. You can add as many as you like, including several for one provider (for example a work key and a personal key), set one as the default, and choose a different model for any single request from the picker under the Studio prompt. Studio remembers the model you last picked or used there, including after you leave Studio or start a new design; the default applies until you pick one.

Connections live in **Settings > AI connections** and take effect immediately.

## Supported connections

| Provider | Method | What you enter | Billing | Where to get it |
| --- | --- | --- | --- | --- |
| Anthropic | Claude API key | API key | Pay per use | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| Anthropic | Claude plan (Claude Code) | Nothing: you sign in to Claude | Uses your plan | A Claude Pro, Max, Team or Enterprise plan. See [below](#claude-plan) |
| OpenAI | OpenAI API key | API key | Pay per use | [platform.openai.com](https://platform.openai.com/api-keys) |
| OpenAI | ChatGPT plan (Sign in with ChatGPT) | Nothing: you sign in to ChatGPT | Uses your plan | A ChatGPT Plus or Pro plan. See [below](#chatgpt-plan) |
| GitHub | Copilot plan (GitHub token) | A fine-grained GitHub token | Uses your plan | A GitHub Copilot plan. See [below](#copilot-plan) |
| Google Gemini | Gemini API key | API key | Pay per use | [aistudio.google.com](https://aistudio.google.com/apikey) |
| OpenRouter | OpenRouter API key | API key | Pay per use | [openrouter.ai](https://openrouter.ai/keys) |
| OpenRouter | Sign in with OpenRouter | Nothing: you sign in on OpenRouter | Pay per use | An OpenRouter account. See [below](#sign-in-with-openrouter) |
| xAI | xAI API key | API key | Pay per use | [console.x.ai](https://console.x.ai) |
| Mistral | Mistral API key | API key | Pay per use | [console.mistral.ai](https://console.mistral.ai/api-keys) |
| Groq | Groq API key | API key | Pay per use | [console.groq.com](https://console.groq.com/keys) |
| DeepSeek | DeepSeek API key | API key | Pay per use | [platform.deepseek.com](https://platform.deepseek.com/api_keys) |
| Amazon Bedrock | IAM access keys, Bedrock API key, or AWS profile | Region and credentials | Cloud account | [AWS console](https://console.aws.amazon.com/bedrock) |
| Google Vertex AI | Service account | Service account key (JSON), project, location | Cloud account | [Google Cloud console](https://console.cloud.google.com/vertex-ai) |
| Azure OpenAI | API key or Entra ID service principal | Endpoint, deployment names, credentials | Cloud account | [Azure portal](https://portal.azure.com) |
| Microsoft Foundry | API key | Models endpoint, model names, API key | Cloud account | [Foundry portal](https://ai.azure.com) |
| Ollama | Ollama | Base URL | Local | Your Ollama server, for example `http://192.168.1.10:11434` |
| OpenAI-compatible | OpenAI-compatible server | Base URL and optional key | Local | LM Studio, vLLM, LocalAI, llama.cpp server, LiteLLM proxy |

Every request records which connection it used and how it is billed. Claude, ChatGPT and GitHub Copilot plans can be used as described below. Other consumer subscriptions cannot be used; [Subscription sign-in by platform](./provider-rules.md) explains why for each one.

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

Pick `sonnet`, `opus`, `haiku` or `fable` from the model picker. Claude Code resolves each to the newest model of that tier; Usage records the exact model. Pickers show the model each alias runs as next to it, for example `sonnet (claude-sonnet-5-5)`, so you can tell which release you are on. Buildplate looks this up whenever the connection is checked (at no token cost) and after each request, so it follows Claude Code as aliases move to newer releases. You can also type a full model ID.

How it works and what it can do:

- Claude Code keeps your sign-in in its own files inside the data folder (`/data/claude-code/<connection>`). Buildplate never reads, copies or stores your Claude credentials; it asks Claude Code whether it is signed in.
- Each Claude plan connection has its own sign-in, so you can add a work plan and a personal plan side by side.
- For each request, Buildplate runs Claude Code with all of its built-in tools turned off (no shell, file or web access) in an empty folder. Claude Code can only call Buildplate's `check_design` and `submit_design`, through a private endpoint that only accepts that request's one-time token.
- Requests use your plan's normal usage limits. Studio and Usage show **Included in plan** instead of a cost, with the API-price equivalent on hover. When the plan's limit is reached, the request stops with a message saying so.
- **Sign out** on the card signs Claude Code out. Removing the connection signs out and deletes its Claude Code folder.
- Buildplate does not resell or pay for Claude usage. You use your own plan under your own agreement with Anthropic.

## ChatGPT plan

The **ChatGPT plan (Sign in with ChatGPT)** connection uses your ChatGPT Plus or Pro plan instead of an API key, through OpenAI's [Sign in with ChatGPT](https://help.openai.com/en/articles/20001542) for open-source apps. You sign in on OpenAI's own page; Buildplate never sees your ChatGPT password.

**Before you start:** set a web UI password and sign in. ChatGPT plan connections always need one, even from the server itself.

1. Under **Add a connection**, choose **OpenAI > ChatGPT plan (Sign in with ChatGPT)**, name it, and select **Continue with ChatGPT**.
2. In the dialog, select the black **Continue with ChatGPT** button. A new tab opens on OpenAI. Sign in, check what Buildplate is asking for and approve.
3. OpenAI then sends the tab to an address starting with `http://127.0.0.1:1455/auth/callback`. That page usually shows a "can't connect" error; this is expected, because the address points at your own computer, not at Buildplate. Copy the whole address from that tab's address bar.
4. Paste it into the box in Buildplate and select **Finish sign-in**. The first time, Buildplate shows **You're using your ChatGPT plan**. Select **Got it**.

The card then shows **Connected** and the models your plan can use appear in every model picker. Studio shows **Using ChatGPT plan** under the prompt when one of them is picked.

How it works and what it can do:

- The sign-in is stored encrypted on your server (`/data/chatgpt/`), readable only by Buildplate. It renews itself; if OpenAI ends it (you disconnected Buildplate in ChatGPT, changed your password, or the plan ended), the card asks you to sign in again.
- Requests use your plan's normal usage limits. Studio and Usage show **Included in plan** instead of a cost, with the API-price equivalent on hover. **Manage usage** opens [ChatGPT's usage settings](https://chatgpt.com/settings/usage), where you can see what Buildplate has used, set a weekly limit for it, or disconnect it.
- When the plan's limit (or a limit you set) is reached, the request stops with a message and a **Manage usage** button.
- Requests are sent with storage turned off, so OpenAI does not keep them as ChatGPT conversations.
- **Sign out** on the card forgets the sign-in. Removing the connection deletes it.
- Buildplate does not resell or pay for ChatGPT usage. You use your own plan under your own agreement with OpenAI.

## Copilot plan

The **Copilot plan (GitHub token)** connection uses your GitHub Copilot plan (Free, Pro, Pro+, Business or Enterprise) instead of an API key. It runs through GitHub's official [Copilot SDK](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate), with a fine-grained token that can only make Copilot requests.

**Before you start:** set a web UI password and sign in. Copilot plan connections always need one, even from the server itself.

1. Under **Add a connection**, choose **GitHub > Copilot plan (GitHub token)**.
2. Select **Create token on GitHub**. GitHub opens its new-token page with the name, a 90-day expiry and the one permission Buildplate needs (**Copilot requests**) already filled in. Check it and select **Generate token**.
3. Copy the token (it starts with `github_pat_`), paste it into Buildplate and select **Connect**.

The first Copilot connection downloads GitHub's Copilot runtime (about 90 MB) from GitHub's releases, checked against GitHub's published checksums, so connecting takes a minute. This needs internet access from the container. The card then shows **Connected** and the models your plan allows appear in every model picker. Studio shows **Using Copilot plan** under the prompt when one of them is picked.

How it works and what it can do:

- The token is stored encrypted, like any other key. Classic tokens (`ghp_`) don't work with Copilot; use a fine-grained one. When it expires or you revoke it on GitHub, the card shows an error: select **Edit** and paste a new token.
- For each request, Buildplate runs GitHub's Copilot runtime with only Buildplate's own design tools (`check_design`, `list_parts`, `submit_design`). It has no shell, file or web access, and doesn't see the server's environment or any other GitHub credential.
- Requests use your plan's premium requests and the models enabled in your [Copilot settings](https://github.com/settings/copilot/features). Studio and Usage show **Included in plan** instead of a cost, with the API-price equivalent on hover where the model's price is known. When you reach your plan's limit, the request stops with a message and a **Manage usage** button.
- Removing the connection deletes its Copilot runtime state. Revoke the token on GitHub's [token page](https://github.com/settings/personal-access-tokens) too.
- Buildplate does not resell or pay for Copilot usage. You use your own plan under your own agreement with GitHub.

## Sign in with OpenRouter

Instead of pasting a key, choose **OpenRouter > Sign in with OpenRouter** and select **Sign in with OpenRouter**. Your browser goes to OpenRouter, you sign in and approve, and OpenRouter sends you back to Buildplate with a new key labelled "Buildplate" that you own. You can see, limit or revoke it on OpenRouter's [keys page](https://openrouter.ai/keys).

The sign-in comes back to the address you opened Buildplate on, or to **Public URL** in **Settings > Access and MCP** if you set one. If OpenRouter cannot send you back (for example the address only works inside your network and OpenRouter rejects it), set Public URL to an address your browser can reach, or paste an API key instead.

## Cloud accounts

Use the AI models in your own AWS, Google Cloud or Azure account. Requests are billed by your cloud provider; Usage shows them as **Cloud account** with the estimated list price.

Connecting a cloud account sends **one tiny test request** (a few tokens, using the cheapest-looking model in the list) to check that the credentials, region and model access all work. If it fails, the card says why: rejected credentials, no access to the model, or a model or deployment that does not exist.

**Amazon Bedrock**

- **IAM access keys**: an access key for an IAM user (or temporary credentials with a session token). Buildplate lists the models and inference profiles your account can use in that region. A user with only the `bedrock:InvokeModel*` and `bedrock:List*` permissions is enough.
- **Bedrock API key**: a key from the Bedrock console. API keys can call models but not list them, so Buildplate suggests models Bedrock offers; pick one your account can use.
- **AWS profile**: a named profile from an AWS config folder you mount into the container at `/data/.home/.aws`.
- Many newer models need a cross-region inference profile ID, such as `us.anthropic...`. Use the IDs Buildplate lists, or type one as a custom model.

**Google Vertex AI**

Create a service account with the **Vertex AI User** role, create a JSON key for it, and paste or load the file. The project comes from the key unless you enter another. Gemini models use regions such as `us-central1`; Claude on Vertex needs a region that offers it, such as `us-east5` or `europe-west1`. Vertex AI express-mode API keys are not supported; use a Gemini API key connection for those models instead.

**Azure OpenAI**

Enter the resource endpoint (`https://<resource>.openai.azure.com`), the deployment names you created (they become the models you can pick), and either the resource's API key or an Entra ID service principal (tenant ID, client ID and client secret, with the **Cognitive Services OpenAI User** role).

**Microsoft Foundry**

For non-OpenAI models deployed in a Foundry project (Llama, Mistral, DeepSeek, Grok and others), enter the project's models endpoint (`https://<project>.services.ai.azure.com/models`), the model or deployment names, and the API key.

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
