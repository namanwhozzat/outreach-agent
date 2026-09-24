# viaSocket outreach agent

Runs on GitHub Actions, Mon–Fri 11:00 IST. Each run it:

1. Checks replies on comments it already posted. If a maintainer replied, it opens an issue in this repo so you get a notification.
2. Searches GitHub for open issues asking for Zapier / n8n / Make / automation / integrations.
3. Filters: 500–60k stars, pushed in last 30 days, has a licence, not a library/SDK/list, not a competitor.
4. Claude reads each issue + thread and decides post or skip. If post, writes a short comment that offers an optional Embed PR and **asks** first.
5. Posts max **2 per run, 7 per week** (hard-coded), one comment per repo ever, disclosure line on every comment.
6. Saves `state.json` and a run report (Actions → run → Summary).

It never opens PRs. When a maintainer says yes, you build the PR.

## Setup (≈15 min)

1. **Create a new private repo**, e.g. `viasocket/outreach-agent`. Upload these files (keep the `.github/workflows/` folder).
2. **GitHub token** — from the account that will comment (a real person or a clearly named viaSocket account):
   github.com → Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate.
   Tick only **`public_repo`**. Copy it.
   (Fine-grained tokens can't comment on repos you don't own — use classic.)
3. **Claude API key** — console.anthropic.com → API keys → Create.
4. In the new repo: **Settings → Secrets and variables → Actions**
   - Secrets: `GH_PAT` = token from step 2, `ANTHROPIC_API_KEY` = key from step 3
   - Variables: `ANTHROPIC_MODEL` = a current model id from docs.claude.com/en/docs/about-claude/models (optional; default `claude-sonnet-4-5`)
5. **Test run (posts nothing):** Actions → "viaSocket outreach agent" → Run workflow → keep "Draft only" ticked. Read the drafts in the run Summary.
6. **Go live:** Settings → Variables → add `LIVE` = `true`. Scheduled runs now post. Delete `LIVE` to stop.

## Stop it
Actions → workflow → "…" → Disable workflow. Or delete the `LIVE` variable (back to draft-only).

## Change behaviour
Top of `agent.py`: search terms (`SEARCH_QUERIES`), star range, caps, product facts (`EMBED_FACTS`).
Update `EMBED_FACTS` if the Embed setup or app count changes — Claude may only say what is in it.
