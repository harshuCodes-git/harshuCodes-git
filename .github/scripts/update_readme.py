"""Regenerate profile-data.json and the auto-managed sections of README.md.

profile-data.json is the single source of truth: the README sections below and
the portfolio site both render from it. Sections are delimited by marker
comments, e.g. <!-- OSS-PRS:START --> ... <!-- OSS-PRS:END -->, and everything
between a pair of markers is overwritten on each run.

Private repos are only visible when PROFILE_TOKEN (a fine-grained PAT) is set.
Because profile-data.json is public, private repos are written according to
config "private_repos": "anonymous" (language + date only), "named" (name and
description, never a link) or "hidden".
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
README = os.path.join(ROOT, "README.md")
DATA = os.path.join(ROOT, "profile-data.json")
CONFIG = os.path.join(ROOT, ".github", "readme-config.json")

USER = os.environ.get("GITHUB_USER", "harshuCodes-git")
PROFILE_TOKEN = os.environ.get("PROFILE_TOKEN", "")
TOKEN = PROFILE_TOKEN or os.environ.get("GITHUB_TOKEN", "")


def api(url):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"{USER}-readme-updater"}
    # Never send the GitHub token anywhere but GitHub.
    if TOKEN and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {TOKEN}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return json.load(resp)


def paginate(url):
    items, page = [], 1
    sep = "&" if "?" in url else "?"
    while True:
        batch = api(f"{url}{sep}per_page=100&page={page}")
        items += batch
        if len(batch) < 100:
            return items
        page += 1


def cell(text):
    """Make text safe for a markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def short(text, limit=100):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "..."


def fetch_contributions(config):
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
        merged_at = (item.get("pull_request") or {}).get("merged_at")
        # Closed without merging isn't a contribution worth showcasing.
        if item["state"] == "closed" and not merged_at:
            continue
        if item["html_url"] in excluded_prs or repo.lower() in excluded_repos:
            continue
        prs.append({
            "repo": repo,
            "repo_url": f"https://github.com/{repo}",
            "title": titles.get(item["html_url"], item["title"]),
            "url": item["html_url"],
            "status": "merged" if merged_at else "open",
            "date": merged_at or item["created_at"],
        })

    prs.sort(key=lambda p: (p["status"] == "merged", p["date"]), reverse=True)
    return prs[: config.get("max_prs", 15)]


def fetch_repos(config):
    if PROFILE_TOKEN:
        repos = paginate("https://api.github.com/user/repos?affiliation=owner&visibility=all")
    else:
        repos = paginate(f"https://api.github.com/users/{USER}/repos?type=owner")

    excluded = {r.lower() for r in config.get("exclude_repos", [])}
    patterns = [re.compile(p, re.I) for p in config.get("exclude_repo_patterns", [])]
    return [
        r for r in repos
        if not r["fork"] and not r["archived"]
        and r["name"].lower() != USER.lower() and r["name"].lower() not in excluded
        and not any(p.search(r["name"]) for p in patterns)
    ]


def raw_file(repo, path):
    try:
        req = urllib.request.Request(
            f"https://raw.githubusercontent.com/{repo}/HEAD/{path}",
            headers={"User-Agent": f"{USER}-readme-updater"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        return None


def readme_summary(text):
    """First prose sentence of a README, skipping headings, badges, HTML, lists and code."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for line in text.splitlines():
        if not line.strip() or line[0].isspace() or line.lstrip()[0] in "#<![|>-*+=`0123456789":
            continue
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)  # [text](url) -> text
        line = re.sub(r"[*_`]", "", line).strip()
        if len(line.split()) >= 5:
            return line
    return ""


def describe(repo, config):
    """Config override, then GitHub description, then package.json, then README."""
    override = {k.lower(): v for k, v in config.get("descriptions", {}).items()}.get(repo["name"].lower())
    if override or repo["description"]:
        return override or repo["description"]
    pkg = raw_file(repo["full_name"], "package.json")
    if pkg:
        try:
            desc = json.loads(pkg).get("description")
            if desc:
                return desc
        except ValueError:
            pass
    readme = raw_file(repo["full_name"], "README.md")
    return readme_summary(readme) if readme else ""


def build_projects(repos, config):
    live_links = {k.lower(): v for k, v in config.get("live_links", {}).items()}
    npm_by_repo = {
        p["repo"].lower(): p["name"] for p in config.get("npm_packages", []) if p.get("repo")
    }
    public = [r for r in repos if not r["private"]]

    # Featured repos first (in config order), then the most recently pushed.
    rank = {name.lower(): i for i, name in enumerate(config.get("featured_repos", []))}
    public.sort(key=lambda r: r["pushed_at"], reverse=True)
    public.sort(key=lambda r: rank.get(r["name"].lower(), len(rank)))

    projects = []
    for r in public[: config.get("max_repos", 10)]:
        npm = npm_by_repo.get(r["name"].lower())
        projects.append({
            "name": r["name"],
            "url": r["html_url"],
            "description": describe(r, config),
            "language": r["language"],
            "topics": r.get("topics", []),
            "stars": r["stargazers_count"],
            # The repo's "Website" field on GitHub wins; config fills in the rest.
            "live": r.get("homepage") or live_links.get(r["name"].lower()),
            "npm": f"https://www.npmjs.com/package/{npm}" if npm else None,
            "featured": r["name"].lower() in rank,
            "pushed_at": r["pushed_at"],
        })
    return projects


def build_private_activity(repos, config):
    settings = config.get("private_repos", {})
    default = settings.get("default", "anonymous")
    overrides = {k.lower(): v for k, v in settings.get("overrides", {}).items()}
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.get("private_activity_days", 60))

    private = [
        r for r in repos
        if r["private"] and datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00")) >= cutoff
    ]
    private.sort(key=lambda r: r["pushed_at"], reverse=True)

    activity = []
    for r in private:
        mode = overrides.get(r["name"].lower(), default)
        if mode == "hidden":
            continue
        named = mode == "named"
        activity.append({
            "name": r["name"] if named else None,
            "description": (r["description"] or "") if named else None,
            "language": r["language"],
            "pushed_at": r["pushed_at"],
        })
    return activity[: config.get("max_private", 5)]


def npm_published(name):
    try:
        api(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}/latest")
        return True
    except urllib.error.HTTPError:
        return False


def build_npm(config):
    packages = [dict(p) for p in config.get("npm_packages", [])]
    # Anything published under the npm account is picked up automatically.
    npm_user = config.get("npm_user")
    if npm_user:
        known = {p["name"] for p in packages}
        data = api(f"https://registry.npmjs.org/-/v1/search?text=maintainer:{npm_user}&size=50")
        for obj in data["objects"]:
            pkg = obj["package"]
            if pkg["name"] not in known:
                packages.append({"name": pkg["name"], "description": pkg.get("description", "")})

    result = []
    for p in packages:
        result.append({
            "name": p["name"],
            "description": p.get("description", ""),
            "url": f"https://www.npmjs.com/package/{p['name']}",
            "repo_url": f"https://github.com/{USER}/{p['repo']}" if p.get("repo") else None,
            "published": npm_published(p["name"]),
        })
    return result


def render_prs(prs):
    badges = {
        "merged": "![merged](https://img.shields.io/badge/merged-8957e5?style=flat-square&logo=git-merge&logoColor=white)",
        "open": "![open](https://img.shields.io/badge/open-2ea44f?style=flat-square&logo=git-pull-request&logoColor=white)",
    }
    lines = [
        "| 📦 Project | 🛠️ Contribution | 📌 Status |",
        "|-----------|-----------------|-----------|",
    ]
    for p in prs:
        lines.append(
            f"| [{cell(p['repo'])}]({p['repo_url']}) | [{cell(p['title'])}]({p['url']}) "
            f"| {badges[p['status']]} |"
        )
    if not prs:
        lines.append("| — | No contributions yet | — |")
    return "\n".join(lines)


def render_projects(projects):
    lines = [
        "| 📁 Repository | 📝 Description | 💻 Language | 🔗 Links | ⭐ Stars |",
        "|--------------|----------------|-------------|----------|---------|",
    ]
    for p in projects:
        links = []
        if p["live"]:
            links.append(f"[🌐&nbsp;Live]({p['live']})")
        if p["npm"]:
            links.append(f"[📦&nbsp;npm]({p['npm']})")
        lines.append(
            f"| [{cell(p['name'])}]({p['url']}) | {cell(short(p['description'])) or '—'} "
            f"| {cell(p['language']) or '—'} | {' · '.join(links) or '—'} | {p['stars']} |"
        )
    return "\n".join(lines)


def render_private(activity):
    if not activity:
        return "<p align=\"center\"><i>Nothing private in progress right now.</i></p>"
    lines = [
        "| 🔒 Project | 💻 Language | 🕒 Last Update |",
        "|-----------|-------------|----------------|",
    ]
    for a in activity:
        name = f"**{cell(a['name'])}**" if a["name"] else "Private project"
        if a.get("description"):
            name += f": {cell(short(a['description'], 80))}"
        date = datetime.fromisoformat(a["pushed_at"].replace("Z", "+00:00")).strftime("%b %d, %Y")
        lines.append(f"| {name} | {cell(a['language']) or '—'} | {date} |")
    return "\n".join(lines)


def render_npm(packages):
    if not packages:
        return "<p align=\"center\"><i>🚧 Packages coming soon...</i></p>"
    lines = [
        "| 📦 Package | 📝 Description | 🏷️ Version | 📥 Downloads |",
        "|-----------|----------------|------------|--------------|",
    ]
    for p in packages:
        if p["published"]:
            version = f"![version](https://img.shields.io/npm/v/{p['name']}?style=flat-square&color=cb3837)"
            downloads = f"![downloads](https://img.shields.io/npm/dm/{p['name']}?style=flat-square)"
        else:
            version = "![status](https://img.shields.io/badge/status-in%20development-orange?style=flat-square)"
            downloads = "—"
        link = p["url"] if p["published"] else (p["repo_url"] or p["url"])
        lines.append(
            f"| [{cell(p['name'])}]({link}) | {cell(p['description']) or '—'} "
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

    repos = fetch_repos(config)
    # Counts only: the Actions log is public for a public repo.
    private_count = sum(r["private"] for r in repos)
    print(f"::notice::PROFILE_TOKEN set: {bool(PROFILE_TOKEN)} | repos: {len(repos)} | private: {private_count}")
    data = {
        "user": USER,
        "contributions": fetch_contributions(config),
        "projects": build_projects(repos, config),
        "private_activity": build_private_activity(repos, config) if PROFILE_TOKEN else [],
        "npm_packages": build_npm(config),
    }
    with open(DATA, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(README, encoding="utf-8") as f:
        text = f.read()
    text = replace_section(text, "OSS-PRS", render_prs(data["contributions"]))
    text = replace_section(text, "OSS-REPOS", render_projects(data["projects"]))
    text = replace_section(text, "OSS-PRIVATE", render_private(data["private_activity"]))
    text = replace_section(text, "OSS-NPM", render_npm(data["npm_packages"]))
    with open(README, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


if __name__ == "__main__":
    main()
