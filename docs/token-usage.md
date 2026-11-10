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

Input grows with each step because the model sees the conversation so far. Within a request, only the latest code, check report and preview image are sent in full: earlier revisions are replaced by a short note (what the check found), so a fix-and-recheck cycle does not resend every earlier draft and image. On a five-cycle run this cuts input tokens roughly in half.

For Claude models (Anthropic API, and Claude on Bedrock, Vertex AI, OpenRouter or Microsoft Foundry), Buildplate marks the system prompt, your request and the already-compacted part of the run for prompt caching, so later steps read them from the cache at the lower cached rate. Other providers (OpenAI, Gemini and most others) cache automatically. Short requests may be below a model's minimum cacheable size, in which case nothing is cached.

**Input tokens** always means all the input the model read, including tokens served from the provider's prompt cache. **Cached** is the part of that input read from the cache, which is billed at a lower rate. This is the same for every connection, so totals can be compared across providers.

## How cost is calculated

- For Anthropic, OpenAI, Gemini and OpenRouter, Buildplate uses LiteLLM's built-in price list, which includes discounts for cached input where the provider offers them.
- For Ollama and other OpenAI-compatible servers, cost is $0.
- For a Claude plan connection, you are not charged per request: requests count against your plan's usage limits instead. Buildplate shows what the same tokens would cost at API list prices, marked **plan**, for example `~$0.0321 plan`. These amounts are kept out of **Pay-per-use cost** and totalled separately under **Claude plan usage**.
- If a model is not in the price list, its cost shows as **n/a**. Add a price in **Settings > Pricing overrides** using the form `provider/model`, for example `openrouter/vendor/model-name`, with prices per million input and output tokens. Overrides also let you match a negotiated rate.

Costs are estimates at list price. Your provider's invoice is the final word.

## Keeping usage low

- **Use sliders, not prompts, for size changes.** Parameter changes re-render locally and cost nothing.
- **Lower the step limit** in **Settings > Design agent** if your model tends to over-iterate.
- **Reduce preview size**, or set **Image previews** to **Never send** for simple parts.
- **Start new designs** for unrelated parts, so earlier conversation is not sent again.
- **Pick a stronger model for hard parts.** Fewer failed attempts often costs less than many cheap ones.

## Claude plan requests

For a Claude plan connection, Buildplate records the token counts Claude Code reports for each response from Claude:

- **Per step**, input and cached tokens come from the start of each response and output tokens from its end. The step's API-price estimate uses LiteLLM's price list, including the higher rate for the 1-hour prompt cache that Claude plans use.
- **Per request**, the totals and the API-price estimate are Claude Code's own figures for the whole request (`modelUsage` in its output). The steps add up to the same token totals.
- **If a request is stopped or fails**, the steps that finished are still recorded and counted.
- **Pricing overrides** for `anthropic/<model>` also apply to plan estimates.

The **Claude plan usage** section on the Usage page shows each signed-in plan's limits as Claude reports them: the current session and weekly meters with their reset times, plus usage credits spent this month if your plan has them. Buildplate gets these from Claude Code's `/usage` report, which does not use model tokens, and refreshes them at most once a minute (select **Refresh** to check now). If a request reaches the plan's limit, it stops with a message that says when the limit resets, and Usage shows a warning when a plan is close to its limit.

Claude plan requests recorded by Buildplate 0.4.0 under-counted input tokens (cached tokens were left out). They are corrected automatically the first time a newer version starts. Their per-step output counts cannot be recovered, but each request's total is correct.

