#!/usr/bin/env python3
"""
viaSocket Embed outreach agent.

Each run:
  1. check   - read replies on comments we already posted; flag maintainer replies.
  2. find    - search GitHub for OPEN issues where users ask for Zapier / n8n / Make /
               automation / integrations, in active open-source SaaS or AI apps.
  3. draft   - Claude reads each issue and decides: post or skip. If post, it writes a
               short, repo-specific comment offering viaSocket Embed and ASKING before any PR.
  4. post    - posts at most MAX_POSTS_PER_RUN comments (hard caps below).
  5. report  - writes state.json + report.md (shown in the GitHub Actions run summary).

It never opens PRs. PR work starts only after a maintainer says yes (status "maintainer_replied").

Env vars:
  GH_PAT             GitHub classic token, scope "public_repo" (needed to comment on others' repos)
  ANTHROPIC_API_KEY  Claude API key
  ANTHROPIC_MODEL    Claude model id (default below; change to a current one from docs.claude.com)
  DRY_RUN            "true" (default) = draft only, post nothing. "false" = post.
"""
import json, os, re, sys, time, datetime as dt, urllib.request, urllib.parse, urllib.error

# ---------------- settings (hard safety caps) ----------------
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN") or "2")
MAX_POSTS_PER_WEEK = 7                 # hard ceiling, cannot be raised by env
MAX_POSTS_PER_RUN = min(MAX_POSTS_PER_RUN, 3)
MIN_STARS, MAX_STARS = 500, 60000
REPO_ACTIVE_DAYS = 30                  # repo must have a push in last N days
ISSUE_MAX_AGE_DAYS = 3 * 365           # ignore issues older than this
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-5"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
REPORT_FILE = os.environ.get("REPORT_FILE", "report.md")
DISCLOSURE = "_Disclosure: I work at viaSocket._"

# Step A: build a pool of real end-user products by GitHub topic (refreshed weekly)
TARGET_TOPICS = ["crm", "helpdesk", "customer-support", "project-management", "task-management", "kanban",
                 "forms", "form-builder", "survey", "email-marketing", "newsletter", "marketing-automation",
                 "invoicing", "accounting", "erp", "hrms", "ats", "scheduling", "booking", "appointment",
                 "live-chat", "chatbot", "ai-assistant", "ai-agents", "knowledge-base", "wiki", "cms",
                 "e-commerce", "lms", "saas", "no-code", "low-code", "internal-tools"]
# Step B: in each pooled repo, look for issues whose TITLE matches any group below.
# GitHub allows max 5 OR per search and 256 characters, so terms are split into groups of 6.
# Edit freely: add groups for new use cases (keep each group to 6 terms max).
SEARCH_GROUPS = {
    "automation tools": 'zapier OR n8n OR "make.com" OR pipedream OR integromat OR ifttt',
    "general":          'integration OR integrations OR automation OR automations OR webhook OR webhooks',
    "use-case verbs":   '"connect to" OR "sync with" OR "send to" OR "push to" OR "export to" OR notify',
    "apps 1":           'slack OR hubspot OR salesforce OR "google sheets" OR mailchimp OR "google calendar"',
    "apps 2":           'notion OR airtable OR whatsapp OR "microsoft teams" OR discord OR telegram',
    "extensibility":    '"third-party" OR "third party" OR plugins OR marketplace OR "api access" OR connectors',
}
assert all(g.count(" OR ") <= 5 for g in SEARCH_GROUPS.values())
REPOS_CHECKED_PER_RUN = 20            # x 6 groups = 120 searches, about 4.5 min at GitHub's 30/min limit
RECHECK_REPO_DAYS = 21
# repos that are libraries/frameworks/lists, not products end users run
EXCLUDE = re.compile(r"\b(framework|library|sdk|toolkit|awesome|curated|list of|tutorial|course|"
                     r"examples?|boilerplate|template|starter|cookbook|wrapper|bindings|plugin for|"
                     r"n8n-nodes|zapier-app)\b", re.I)
# automation tools themselves (competitors) - never target
COMPETITOR_REPOS = {"n8n-io/n8n", "activepieces/activepieces", "windmill-labs/windmill",
                    "huginn/huginn", "automatisch/automatisch", "PipedreamHQ/pipedream",
                    "ComposioHQ/composio", "NangoHQ/nango", "viasocket/viasocket"}
MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
BAD_TOPICS = {"homelab", "home-lab", "home-automation", "homeassistant", "home-assistant", "raspberry-pi",
              "offline-first", "local-first", "nut", "ups", "desktop-app", "cli", "vscode-extension",
              # frameworks / libraries for developers, not products end users run
              "framework", "library", "sdk", "agent-framework", "llm-framework", "multi-agent-framework",
              "python-library", "npm-package", "langchain", "langgraph", "api-wrapper", "boilerplate", "template",
              "mcp-server", "developer-tools", "devtools", "awesome", "awesome-list"}
# drafts containing any of these are rejected (hype, vague claims, promises we can't back)
BANNED = ["!", "etc.", "many ", "and more", "looks great", "sync", "real-time", "realtime", "seamless",
          "easily", "powerful", "free", "self-host", "leverage"]

EMBED_FACTS = """
Facts about viaSocket Embed (use ONLY these; do not invent pricing, free tiers, self-hosting, or numbers):
- viaSocket Embed lets a product show viaSocket's app integrations and automations inside its own UI.
- The catalog has 2,300+ app integrations (Gmail, Slack, HubSpot, Google Sheets, etc.).
- End users connect apps and build automations without leaving the host product.
- Setup for the host app: viaSocket org_id, project_id, access_key; the host backend signs a JWT (HS256)
  per user and passes it as embedToken to the viaSocket embed script.
- viaSocket itself is a hosted (cloud) service. Self-hostable web apps CAN still use Embed (the app loads the viaSocket
  script over the internet). Only treat it as a bad fit if the issue says everything must stay offline / on-premise /
  without third-party cloud services.
- The PR we would offer: optional, off by default, enabled by env vars; one backend route to sign the token;
  one UI entry point; docs. Nothing changes for users who don't enable it.
"""

# ---------------- http ----------------
_last_search = [0.0]

def http(method, url, headers, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "replace")
                return json.loads(txt) if txt else {}
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code in (403, 429) and ("rate limit" in msg.lower() or e.code == 429):
                wait = int(e.headers.get("Retry-After") or 60)
                log(f"rate limited, sleeping {wait}s")
                time.sleep(min(wait, 120)); continue
            if e.code in (404, 410, 422):
                return None
            if e.code >= 500:
                time.sleep(5); continue
            raise RuntimeError(f"{method} {url} -> {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError) as ex:
            log(f"network error {ex}; retry"); time.sleep(5)
    return None

def gh(path, params=None, method="GET", body=None, search=False):
    url = path if path.startswith("http") else "https://api.github.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if search:  # 30 searches/min authenticated -> keep 2.2s gap
        gap = 2.2 - (time.time() - _last_search[0])
        if gap > 0: time.sleep(gap)
        _last_search[0] = time.time()
    return http(method, url, {
        "Authorization": f"Bearer {os.environ['GH_PAT']}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "viasocket-outreach-agent"}, body)

def claude(system, user, max_tokens=900):
    res = http("POST", "https://api.anthropic.com/v1/messages", {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"},
        {"model": MODEL, "max_tokens": max_tokens, "system": system,
         "messages": [{"role": "user", "content": user}]}, timeout=120)
    if not res:
        raise RuntimeError("Claude API returned nothing")
    return "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text")

# ---------------- viaSocket app catalog ----------------
CATALOG_URL = os.environ.get("CATALOG_URL") or "https://flow.sokt.io/func/scriVcDYhP1N"
CORE_APPS = {"gmail", "slack", "hubspot", "google sheets"}
GENERIC = {"feature", "features", "integration", "integrations", "webhook", "webhooks", "api", "support",
           "request", "requests", "inbox", "automation", "automations", "notification", "notifications",
           "sync", "export", "import", "plugin", "plugins", "add", "allow", "enhancement", "bug", "the", "with",
           "send", "connect", "push", "notify", "create", "enable", "option", "options", "native", "using",
           "when", "should", "would", "please", "provider", "providers", "channel", "channels", "service",
           "services", "app", "apps", "tool", "tools", "custom", "external", "third", "party", "data"}
_catalog = [None]
CHECKED_THIS_RUN = []

def catalog():
    """Set of lowercase app names in viaSocket's catalog, or empty set if it can't be loaded."""
    if _catalog[0] is not None:
        return _catalog[0]
    names = set()
    try:
        with urllib.request.urlopen(urllib.request.Request(CATALOG_URL, headers={"User-Agent": "viasocket-outreach-agent"}), timeout=90) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k.lower() in ("name", "title", "appname", "app_name", "pluginname", "plugin_name") and isinstance(v, str):
                        names.add(v.strip().lower())
                    else:
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    if isinstance(v, str): names.add(v.strip().lower())
                    else: walk(v)
        walk(data)
    except Exception as ex:
        log(f"could not load app catalog ({ex}); only Gmail/Slack/HubSpot/Google Sheets may be named")
    names = {n for n in names if len(n) >= 3 and not any(t in n for t in ("test", "dummy", "asdf"))}
    log(f"app catalog: {len(names)} apps")
    _catalog[0] = names
    return names

def catalog_apps_in(text):
    low = text.lower()
    return sorted(n for n in catalog() if len(n) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", low))

# ---------------- state ----------------
def log(*a): print(*a, file=sys.stderr, flush=True)

def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"me": None, "contacted_repos": {}, "skipped_issues": {}, "posts": []}

def save_state(s):
    if DRY_RUN:   # test runs never change memory, so the live run starts clean
        return
    json.dump(s, open(STATE_FILE, "w"), indent=1, sort_keys=True)

def now(): return dt.datetime.now(dt.timezone.utc)

def posts_last_7d(s):
    cut = now() - dt.timedelta(days=7)
    return sum(1 for p in s["posts"] if not p.get("dry_run") and dt.datetime.fromisoformat(p["posted_at"]) > cut)

# ---------------- 1. check replies ----------------
def check_replies(s):
    updates = []
    for p in s["posts"]:
        if p.get("dry_run") or p["status"] not in ("posted", "reply_from_user"):
            continue
        comments = gh(f"/repos/{p['repo']}/issues/{p['issue_number']}/comments", {"per_page": 100}) or []
        after = [c for c in comments if c["created_at"] > p["posted_at_gh"] and c["user"]["login"] != s["me"]]
        maint = [c for c in after if c.get("author_association") in MAINTAINER]
        issue = gh(f"/repos/{p['repo']}/issues/{p['issue_number']}") or {}
        if maint:
            p["status"] = "maintainer_replied"
            p["reply"] = {"by": maint[0]["user"]["login"], "url": maint[0]["html_url"], "text": maint[0]["body"][:500]}
            updates.append(p)
        elif after:
            p["status"] = "reply_from_user"
        if issue.get("state") == "closed" and p["status"] != "maintainer_replied":
            p["status"] = "issue_closed"
            updates.append(p)
        if p["status"] == "posted" and now() - dt.datetime.fromisoformat(p["posted_at"]) > dt.timedelta(days=30):
            p["status"] = "no_reply_30d"
    return updates

# ---------------- 2. find ----------------
def repo_ok(r):
    if not r or r.get("archived") or r.get("fork") or r.get("private"):
        return False, "archived/fork/private"
    if r["full_name"] in COMPETITOR_REPOS: return False, "competitor"
    if not (MIN_STARS <= r["stargazers_count"] <= MAX_STARS): return False, "stars out of range"
    if EXCLUDE.search((r.get("description") or "") + " " + r["name"]): return False, "library/list"
    if not r.get("license"): return False, "no licence"
    pushed = dt.datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
    if now() - pushed > dt.timedelta(days=REPO_ACTIVE_DAYS): return False, "inactive"
    if not r.get("has_issues", True): return False, "issues off"
    if set(t.lower() for t in r.get("topics", [])) & BAD_TOPICS: return False, "homelab/offline tool"
    return True, ""

def refresh_pool(s):
    pool = s.setdefault("repo_pool", {"built": None, "repos": {}})
    if pool["built"] and now() - dt.datetime.fromisoformat(pool["built"]) < dt.timedelta(days=7):
        return
    pushed = (now() - dt.timedelta(days=REPO_ACTIVE_DAYS)).date().isoformat()
    for t in TARGET_TOPICS:
        q = f"topic:{t} stars:{MIN_STARS}..{MAX_STARS} pushed:>{pushed} archived:false fork:false"
        res = gh("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": 100}, search=True) or {}
        for r in res.get("items", []):
            ok, _ = repo_ok(r)
            if ok:
                pool["repos"][r["full_name"]] = {
                    "stars": r["stargazers_count"], "language": r.get("language"),
                    "license": (r.get("license") or {}).get("spdx_id"),
                    "description": r.get("description") or "", "topics": r.get("topics", [])}
    pool["built"] = now().isoformat()
    log(f"repo pool: {len(pool['repos'])} products")

def find_candidates(s, limit=25):
    refresh_pool(s)
    checked = s.setdefault("repo_checked", {})
    cut = now() - dt.timedelta(days=RECHECK_REPO_DAYS)
    pool = s["repo_pool"]["repos"]
    # Sweet spot first: 1k-20k stars (big enough to matter, small enough to answer), then the rest.
    # Shuffled inside each band so every run (including test runs) looks at different products.
    import random
    eligible = [r for r in pool if r not in s["contacted_repos"]
                and (r not in checked or dt.datetime.fromisoformat(checked[r]) < cut)
                and not (set(t.lower() for t in pool[r].get("topics", [])) & BAD_TOPICS)]
    sweet = [r for r in eligible if 1000 <= pool[r]["stars"] <= 20000]
    rest = [r for r in eligible if r not in sweet]
    random.shuffle(sweet); random.shuffle(rest)
    todo = sweet + rest
    # leftovers from last run (found but not reached because of the daily cap) go first
    out = [c for c in s.pop("queue", []) if c["repo"] not in s["contacted_repos"]
           and f"{c['repo']}#{c['issue_number']}" not in s["skipped_issues"]]
    min_created = (now() - dt.timedelta(days=ISSUE_MAX_AGE_DAYS)).date().isoformat()
    for repo in todo[:(30 if DRY_RUN else REPOS_CHECKED_PER_RUN)]:
        hits = {}
        for label, terms in SEARCH_GROUPS.items():
            q = f"repo:{repo} is:issue is:open {terms} in:title created:>{min_created}"
            res = gh("/search/issues", {"q": q, "sort": "reactions", "order": "desc", "per_page": 5}, search=True)
            if res is None:
                log(f"search rejected by GitHub for group '{label}' - check its syntax")
                continue
            for it in res.get("items", []):
                h = hits.setdefault(it["number"], {**it, "matched": []})
                h["matched"].append(label)
        checked[repo] = now().isoformat()
        CHECKED_THIS_RUN.append(repo)
        r = pool[repo]
        best = sorted(hits.values(), key=lambda i: (len(i["matched"]), i.get("reactions", {}).get("total_count", 0)), reverse=True)
        for it in best[:3]:                          # top 3 issues per repo
            key = f"{repo}#{it['number']}"
            if "pull_request" in it or it.get("locked") or key in s["skipped_issues"]:
                continue
            out.append({"repo": repo, "issue_number": it["number"], "issue_url": it["html_url"],
                        "title": it["title"], "body": (it.get("body") or "")[:3000],
                        "reactions": it.get("reactions", {}).get("total_count", 0),
                        "comments": it.get("comments", 0), "created_at": it["created_at"],
                        "matched": it["matched"], **r})
    out.sort(key=lambda c: (c["reactions"] * 3 + c["comments"], c["stars"]), reverse=True)
    log(f"pool {len(pool)} products, {len(eligible)} eligible; checked {min(len(todo), 30 if DRY_RUN else REPOS_CHECKED_PER_RUN)} repos, {len(out)} candidate issues")
    return out[:limit]

# ---------------- 3. draft ----------------
SYSTEM = f"""You help viaSocket do honest, low-volume developer outreach on GitHub.
You read one GitHub issue and decide whether a comment offering viaSocket Embed would be genuinely
useful to that project, then write it.
{EMBED_FACTS}
Rules:
- Only say post=true if the issue asks to connect the product to other apps (e.g. send to Slack, add to
  HubSpot, export to Google Sheets, notify on WhatsApp), or for automation / integrations / Zapier / n8n / Make,
  AND the repo is an end-user product (SaaS app, CRM, helpdesk, PM tool, AI app), not a library.
- post=false if: a maintainer already rejected integrations, a maintainer is already building / has a PR
  for the ask, the issue is a bug report, the project already shipped what is asked, the project is a
  homelab / hardware / offline-first / desktop / CLI tool (self-hostable web apps ARE fine targets), or the thread says no vendors/ads.
- Comment: plain English, under 80 words, one short paragraph plus the question. No hype, no emojis, no links.
  Reference the specific ask in this issue. Offer an OPTIONAL, off-by-default PR. End by ASKING the
  maintainers if they would accept it. Never claim anything outside the facts above.
- Name NO apps except Gmail, Slack, HubSpot, Google Sheets, plus any app listed under "Supported apps named
  in this issue" in the message. Never say viaSocket supports an app the issue names if it is not in that list.
- If the app the issue mainly asks for is NOT in the supported list, set post=false. Do not promise syncs, 2-way sync,
  real-time, or specific features; say "automations between apps".
- Do NOT include a disclosure line; the system adds it.
Reply with ONLY JSON: {{"post": true|false, "reason": "<one line>", "comment": "<markdown or empty>"}}"""

def draft(c, s):
    thread = gh(f"/repos/{c['repo']}/issues/{c['issue_number']}/comments", {"per_page": 30}) or []
    convo = "\n".join(f"- {x['user']['login']} ({x.get('author_association')}): {(x.get('body') or '')[:400]}" for x in thread)
    if any(x["user"]["login"] == s["me"] for x in thread):
        return {"post": False, "reason": "we already commented", "comment": ""}
    user = (f"Repo: {c['repo']} ({c['stars']} stars, {c['language']}, licence {c['license']})\n"
            f"Repo description: {c['description']}\nTopics: {', '.join(c['topics'])}\n\n"
            f"Issue #{c['issue_number']}: {c['title']}\nMatched keyword groups: {', '.join(c.get('matched', []))}\nOpened: {c['created_at']}  Reactions: {c['reactions']}\n"
            f"Body:\n{c['body']}\n\nExisting comments:\n{convo or '(none)'}")
    supported = catalog_apps_in(c["title"] + "\n" + c["body"])
    user += "\n\nSupported apps named in this issue (from viaSocket's catalog): " + (", ".join(supported) or "none found")
    d = ask(user)
    problem = check(d, c, supported)
    if problem:  # one rewrite attempt with the exact problem
        d = ask(user + f"\n\nYour previous draft was rejected: {problem}. Previous draft:\n{d['comment']}\n"
                       "Rewrite it to fix exactly that, following every rule.")
        problem = check(d, c, supported)
        if problem:
            return {"post": False, "reason": f"draft failed checks twice ({problem})", "comment": d.get("comment", "")}
    return d

def ask(user):
    raw = claude(SYSTEM, user)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        d = {}
    if not isinstance(d, dict) or "post" not in d:
        return {"post": False, "reason": "model output unreadable", "comment": ""}
    d["comment"] = (d.get("comment") or "").strip()
    return d

def check(d, cand=None, supported=()):
    if not d.get("post"):
        return None
    c = d["comment"]
    if cand:  # an app named in the issue title may appear in the comment only if viaSocket supports it
        ok = set(CORE_APPS) | set(supported) | set(re.split(r"[/_\-]", cand["repo"].lower()))
        title = re.sub(r"^\s*(\[[^\]]*\]\s*:?\s*|\w+\s*:\s*)", "", cand["title"])   # drop "[feature]:" / "feat:"
        words = re.findall(r"\b[A-Z][A-Za-z0-9.+]{3,}\b", title)
        for w in words:
            lw = w.lower()
            if lw in GENERIC or lw in ok: continue
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", c, re.I):
                return f"names '{w}', which is not confirmed in viaSocket's app catalog"
    if len(c.split()) > 90: return f"too long ({len(c.split())} words, max 90)"
    if "?" not in c: return "no question to the maintainers"
    hit = [b for b in BANNED if b in c.lower()]
    if hit: return "banned words: " + ", ".join(repr(h) for h in hit)
    return None

# ---------------- 4. post ----------------
def post_comment(s, c, text):
    body = f"{DISCLOSURE}\n\n{text}"
    rec = {"repo": c["repo"], "issue_number": c["issue_number"], "issue_url": c["issue_url"],
           "title": c["title"], "comment": body, "posted_at": now().isoformat(), "dry_run": DRY_RUN}
    if DRY_RUN:
        rec.update(status="draft_only", comment_url=None, posted_at_gh=None)
    else:
        res = gh(f"/repos/{c['repo']}/issues/{c['issue_number']}/comments", method="POST", body={"body": body})
        if not res or "html_url" not in res:
            raise RuntimeError(f"posting failed on {c['issue_url']}")
        rec.update(status="posted", comment_url=res["html_url"], posted_at_gh=res["created_at"])
        s["contacted_repos"][c["repo"]] = rec["posted_at"]
    s["posts"].append(rec)
    return rec

# ---------------- 5. report ----------------
def report(s, updates, new, skipped):
    L = [f"# viaSocket outreach run — {now():%Y-%m-%d %H:%M} UTC", f"Mode: {'DRY RUN (nothing posted)' if DRY_RUN else 'LIVE'}", ""]
    if updates:
        L += ["## Needs you (maintainer replied / closed)"]
        for p in updates:
            r = p.get("reply")
            L.append(f"- **{p['status']}** {p['repo']} — [{p['title']}]({p['issue_url']})" +
                     (f" — {r['by']}: \"{r['text'][:200]}\" ([reply]({r['url']}))" if r else ""))
        L.append("")
    L += ["## Posted this run" if not DRY_RUN else "## Drafted this run (not posted)"]
    for p in new:
        L += [f"- {p['repo']} — [{p['title']}]({p['issue_url']})" + (f" → [comment]({p['comment_url']})" if p.get("comment_url") else ""),
              "", "  > " + p["comment"].replace("\n", "\n  > "), ""]
    if not new: L.append("- none")
    L += ["", f"## Products checked this run ({len(CHECKED_THIS_RUN)})", ", ".join(CHECKED_THIS_RUN) or "none"]
    L += ["", f"App catalog loaded: {len(_catalog[0] or [])} apps" + ("" if _catalog[0] else " — NOT loaded, only Gmail/Slack/HubSpot/Google Sheets allowed")]
    L += ["", "## Skipped by Claude"] + [f"- {k}: {v}" for k, v in skipped] + ([] if skipped else ["- none"])
    counts = {}
    for p in s["posts"]: counts[p["status"]] = counts.get(p["status"], 0) + 1
    L += ["", "## All-time status", *[f"- {k}: {v}" for k, v in sorted(counts.items())]]
    open(REPORT_FILE, "w").write("\n".join(L) + "\n")
    log("\n".join(L))

def main():
    s = load_state()
    if not s.get("me"):
        s["me"] = (gh("/user") or {}).get("login")
        if not s["me"]: sys.exit("GH_PAT invalid: /user returned nothing")
    log(f"account: {s['me']}  dry_run: {DRY_RUN}  model: {MODEL}")
    catalog()                       # load app list up front so the report can show whether it worked
    updates = check_replies(s)
    budget = 5 if DRY_RUN else min(MAX_POSTS_PER_RUN, MAX_POSTS_PER_WEEK - posts_last_7d(s))  # test runs show 5 drafts
    new, skipped = [], []
    if budget > 0:
        cands = find_candidates(s)
        done_repos = set()
        for i, c in enumerate(cands):
            if len(new) >= budget:
                s["queue"] = [x for x in cands[i:] if x["repo"] not in done_repos]   # keep the rest for the next run
                break
            if c["repo"] in done_repos:
                continue          # one comment per repo, ever - never two issues of the same repo
            key = f"{c['repo']}#{c['issue_number']}"
            d = draft(c, s)
            if not d.get("post"):
                s["skipped_issues"][key] = d.get("reason", "skip"); skipped.append((key, d.get("reason")))
                continue
            new.append(post_comment(s, c, d["comment"]))
            done_repos.add(c["repo"])
            save_state(s)
            time.sleep(20)  # space posts out
    else:
        log("weekly cap reached; posting nothing")
    save_state(s)
    report(s, updates, new, skipped)
    notify = os.environ.get("NOTIFY_REPO")  # e.g. "viasocket/outreach-agent" -> opens an issue so you get a GitHub notification
    if notify and updates and not DRY_RUN:
        gh(f"/repos/{notify}/issues", method="POST", body={
            "title": f"Outreach: {len(updates)} repo(s) need you ({now():%d %b})",
            "body": open(REPORT_FILE).read()[:60000]})

if __name__ == "__main__":
    main()
