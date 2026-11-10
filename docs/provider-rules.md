# Subscription sign-in by platform

**Last checked: 2026-11-10.** These rules change often. They are checked again before each release.

Many people already pay for an AI chat subscription and would like Buildplate to use it instead of a pay-per-use API key. Buildplate only does this where the vendor offers an approved way for other apps to use the plan. It never copies a login out of a vendor's app or website, because that breaks the vendor's terms and can get your account restricted.

| Platform | Subscription | Third-party sign-in? | Buildplate | Source |
| --- | --- | --- | --- | --- |
| Anthropic Claude | Pro, Max, Team, Enterprise | Yes, through Anthropic's own Claude Code app | **Built:** [Claude plan](./providers.md#claude-plan) | [Claude Code](https://code.claude.com) |
| OpenAI ChatGPT | Plus, Pro (and other plans OpenAI includes) | Yes: Sign in with ChatGPT, open to self-hosted open-source apps | **Built:** [ChatGPT plan](./providers.md#chatgpt-plan) | [OpenAI: Sign in with ChatGPT for open source](https://developers.openai.com/siwc/token-sharing-open-source), [help article](https://help.openai.com/en/articles/20001542) |
| GitHub Copilot | Free, Pro, Pro+, Business, Enterprise | Yes: the Copilot SDK, with a fine-grained token that has the Copilot requests permission | **Built:** [Copilot plan](./providers.md#copilot-plan) | [Copilot SDK authentication](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate), [Copilot CLI authentication](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli) |
| xAI Grok | X / SuperGrok | Some apps offer it, but xAI has not published a program for third-party apps | **Waiting** for xAI to document one. Use an [xAI API key](https://console.x.ai) | [Warp](https://docs.warp.dev/agents/inference/grok-subscription/), [LobeHub](https://lobehub.com/docs/usage/providers/supergrok) (third-party, not xAI) |
| Mistral | Le Chat / Vibe Pro, Team | No. Plan usage is for Mistral's own apps | **Not allowed.** Use a [Mistral API key](https://console.mistral.ai/api-keys) | [Mistral Vibe 2.0](https://mistral.ai/fr/news/mistral-vibe-2-0) |
| Google Gemini | Google AI Pro, Ultra | No. Using the Gemini CLI or Antigravity sign-in from other software breaks Google's terms, and accounts have been restricted for it | **Not allowed.** Use a [Gemini API key](https://aistudio.google.com/apikey) or [Vertex AI](./providers.md#cloud-accounts) | [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/), [Google AI forum](https://discuss.ai.google.dev/t/gemini-subscription-access-for-third-party-apps-is-a-sanctioned-program-planned/175577) |

## Why can't I use my Gemini subscription?

Google AI Pro and Ultra cover Google's own apps (the Gemini app, Gemini CLI, Antigravity). Google does not offer a way for other apps to use them, and its Gemini CLI FAQ says that using its sign-in from third-party software is against the terms. Some tools do it anyway; Google has restricted accounts that used them. Buildplate will not put your Google account at risk.

To use Gemini models, add a **Gemini API key** (Google AI Studio has a free tier) or a **Google Vertex AI** connection on your own Google Cloud account.

## How Buildplate decides

- **Only the vendor's approved path.** The vendor runs the sign-in, on its own page or in its own app.
- **Store as little as possible.** Where the vendor's program has Buildplate keep a sign-in or token (ChatGPT, Copilot), it is encrypted on your server. Where the vendor's app keeps it (Claude Code), Buildplate never reads its files.
- **Clear billing.** Plan requests are shown as **Included in plan**, separately from pay-per-use cost.
- **Password required.** Plan connections need a web UI password, because they spend your subscription.

If a vendor publishes a program for third-party apps, open an issue and it will be added.
