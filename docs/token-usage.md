# Token usage and cost

Every request to an AI model is recorded with its token counts and estimated cost, so you always know what a design cost to make.

## Where you see it

- **In Studio**, under each answer: input tokens, output tokens, cached tokens (when the provider reports them), cost, and the number of steps.
- **Below the prompt**: the running total for your current session.
- **On the Usage page**: totals for the selected period, a daily chart, a breakdown by model, and a list of every request. Select a request to see each step of the design loop with its own token counts, the tools it called and how long it took.
- **As CSV**: **Usage > Export CSV** downloads every request.

Requests made through the MCP `generate_design` tool are tracked the same way and marked as coming from MCP.

## What counts as a step

One request usually takes two to four model calls. A typical run:

| Step | What the model does | Typical tokens |
| --- | --- | --- |
| 1 | Reads your request and writes the first draft | Small input, large output |
| 2 | Reads the check report and preview, fixes problems | Larger input (includes the image) |
| 3 | Confirms and submits | Larger input, small output |

Input grows with each step because the model sees the whole conversation so far, including earlier code and previews.

## How cost is calculated

- For Anthropic, OpenAI, Gemini and OpenRouter, Buildplate uses LiteLLM's built-in price list, which includes discounts for cached input where the provider offers them.
- For Ollama and other OpenAI-compatible servers, cost is $0.
- If a model is not in the price list, its cost shows as **n/a**. Add a price in **Settings > Pricing overrides** using the form `provider/model`, for example `openrouter/vendor/model-name`, with prices per million input and output tokens. Overrides also let you match a negotiated rate.

Costs are estimates at list price. Your provider's invoice is the final word.

## Keeping usage low

- **Use sliders, not prompts, for size changes.** Parameter changes re-render locally and cost nothing.
- **Lower the step limit** in **Settings > Design agent** if your model tends to over-iterate.
- **Reduce preview size**, or set **Image previews** to **Never send** for simple parts.
- **Start new designs** for unrelated parts, so earlier conversation is not sent again.
- **Pick a stronger model for hard parts.** Fewer failed attempts often costs less than many cheap ones.
