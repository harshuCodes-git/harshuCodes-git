"""Regenerate the auto-managed sections of README.md from the GitHub API.

Sections are delimited by marker comments, e.g.
<!-- OSS-PRS:START --> ... <!-- OSS-PRS:END -->
Everything between a pair of markers is overwritten on each run.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
README = os.path.join(ROOT, "README.md")
CONFIG = os.path.join(ROOT, ".github", "readme-config.json")

USER = os.environ.get("GITHUB_USER", "harshuCodes-git")
TOKEN = os.environ.get("GITHUB_TOKEN", "")


def api(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-readme-updater",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def cell(text):
    """Make text safe for a markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def fetch_prs(config):
    query = urllib.parse.quote(f"is:pr author:{USER} -user:{USER}")
    items, page = [], 1
    while True:
        data = api(f"https://api.github.com/search/issues?q={query}&per_page=100&page={page}")
        items += data["items"]
        if len(items) >= data["total_count"] or not data["items"] or page >= 10:
            break
        page += 1

    excluded_prs = set(config.get("exclude_prs", []))
    excluded_repos = {r.lower() for r in config.get("exclude_contribution_repos", [])}
    titles = config.get("pr_titles", {})

    prs = []
    for item in items:
        repo = item["repository_url"].split("/repos/", 1)[1]
        merged = bool((item.get("pull_request") or {}).get("merged_at"))
        # Closed without merging isn't a contribution worth showcasing.
        if item["state"] == "closed" and not merged:
            continue
        if item["html_url"] in excluded_prs or repo.lower() in excluded_repos:
            continue
        prs.append({
            "repo": repo,
            "url": item["html_url"],
            "title": titles.get(item["html_url"], item["title"]),
            "merged": merged,
            "date": item["pull_request"].get("merged_at") or item["created_at"],
        })

    prs.sort(key=lambda p: (p["merged"], p["date"]), reverse=True)
    return prs[: config.get("max_prs", 15)]


def render_prs(prs):
    merged = "![merged](https://img.shields.io/badge/merged-8957e5?style=flat-square&logo=git-merge&logoColor=white)"
    opened = "![open](https://img.shields.io/badge/open-2ea44f?style=flat-square&logo=git-pull-request&logoColor=white)"
    lines = [
        "| 📦 Project | 🛠️ Contribution | 📌 Status |",
        "|-----------|-----------------|-----------|",
    ]
    for p in prs:
        lines.append(
            f"| [{cell(p['repo'])}](https://github.com/{p['repo']}) "
            f"| [{cell(p['title'])}]({p['url']}) "
            f"| {merged if p['merged'] else opened} |"
        )
    if not prs:
        lines.append("| — | No contributions yet | — |")
    return "\n".join(lines)


def fetch_repos(config):
    repos, page = [], 1
    while True:
        batch = api(f"https://api.github.com/users/{USER}/repos?type=owner&per_page=100&page={page}")
        repos += batch
        if len(batch) < 100:
            break
        page += 1

    excluded = {r.lower() for r in config.get("exclude_repos", [])}
    patterns = [re.compile(p, re.I) for p in config.get("exclude_repo_patterns", [])]
    repos = [
        r for r in repos
        if not r["fork"] and not r["archived"] and not r["private"]
        and r["name"].lower() != USER.lower() and r["name"].lower() not in excluded
        and not any(p.search(r["name"]) for p in patterns)
    ]
    # Featured repos first (in config order), then the most recently pushed.
    featured = [f.lower() for f in config.get("featured_repos", [])]
    rank = {name: i for i, name in enumerate(featured)}
    repos.sort(key=lambda r: r["pushed_at"], reverse=True)
    repos.sort(key=lambda r: rank.get(r["name"].lower(), len(rank)))
    return repos[: config.get("max_repos", 10)]


def render_repos(repos, config):
    live_links = {k.lower(): v for k, v in config.get("live_links", {}).items()}
    npm_by_repo = {
        p["repo"].lower(): p["name"] for p in config.get("npm_packages", []) if p.get("repo")
    }
    lines = [
        "| 📁 Repository | 📝 Description | 💻 Language | 🔗 Links | ⭐ Stars |",
        "|--------------|----------------|-------------|----------|---------|",
    ]
    for r in repos:
        desc = r["description"] or ""
        if len(desc) > 100:
            desc = desc[:97].rstrip() + "..."
        # The repo's "Website" field on GitHub wins; config fills in the rest.
        live = r.get("homepage") or live_links.get(r["name"].lower())
        links = []
        if live:
            links.append(f"[🌐 Live]({live})")
        if r["name"].lower() in npm_by_repo:
            pkg = npm_by_repo[r["name"].lower()]
            links.append(f"[📦 npm](https://www.npmjs.com/package/{pkg})")
        lines.append(
            f"| [{cell(r['name'])}]({r['html_url']}) | {cell(desc) or '—'} "
            f"| {cell(r['language']) or '—'} | {' · '.join(links) or '—'} "
            f"| {r['stargazers_count']} |"
        )
    return "\n".join(lines)


def npm_published(name):
    try:
        api(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}/latest")
        return True
    except urllib.error.HTTPError:
        return False


def render_npm(config):
    packages = list(config.get("npm_packages", []))
    # Anything published under the npm account is picked up automatically.
    npm_user = config.get("npm_user")
    if npm_user:
        known = {p["name"] for p in packages}
        data = api(f"https://registry.npmjs.org/-/v1/search?text=maintainer:{npm_user}&size=50")
        for obj in data["objects"]:
            pkg = obj["package"]
            if pkg["name"] not in known:
                packages.append({"name": pkg["name"], "description": pkg.get("description", "")})

    if not packages:
        return "<p align=\"center\"><i>🚧 Packages coming soon...</i></p>"

    lines = [
        "| 📦 Package | 📝 Description | 🏷️ Version | 📥 Downloads |",
        "|-----------|----------------|------------|--------------|",
    ]
    for p in packages:
        name = p["name"]
        if npm_published(name):
            version = f"![version](https://img.shields.io/npm/v/{name}?style=flat-square&color=cb3837)"
            downloads = f"![downloads](https://img.shields.io/npm/dm/{name}?style=flat-square)"
        else:
            version = "![status](https://img.shields.io/badge/status-in%20development-orange?style=flat-square)"
            downloads = "—"
        link = f"https://www.npmjs.com/package/{name}"
        if p.get("repo"):
            link = f"https://github.com/{USER}/{p['repo']}"
        lines.append(
            f"| [{cell(name)}]({link}) | {cell(p.get('description')) or '—'} "
            f"| {version} | {downloads} |"
        )
    return "\n".join(lines)


def replace_section(text, name, content):
    pattern = re.compile(rf"(<!-- {name}:START -->)(.*?)(<!-- {name}:END -->)", re.S)
    if not pattern.search(text):
        sys.exit(f"Marker {name} not found in README.md")
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", text)


def main():
    with open(CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    with open(README, encoding="utf-8") as f:
        text = f.read()

    text = replace_section(text, "OSS-PRS", render_prs(fetch_prs(config)))
    text = replace_section(text, "OSS-REPOS", render_repos(fetch_repos(config), config))
    text = replace_section(text, "OSS-NPM", render_npm(config))

    with open(README, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if __name__ == "__main__":
    main()
