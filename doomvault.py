#!/usr/bin/env python3
"""
DoomVault — Double-click to run. Opens in your browser.
Archives any GitHub repo fully offline: git history, releases, assets.

Copyright 2026 [David Coe]

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import http.server, json, os, re, shutil, socketserver
import subprocess, sys, tarfile, threading, time
import urllib.request, urllib.error, webbrowser
from datetime import datetime
from pathlib import Path

# ─── Globals ──────────────────────────────────────────────────────────────────

log_lines        = []
archive_running  = False
cancel_requested = False
progress_state   = {"pct": 0, "step": "", "bytes_done": 0, "bytes_total": 0, "dl_start": 0}
last_ping        = 0.0
browser_connected = False
# Store the vault list in the user's home dir so it survives no matter
# where the script is launched from (Downloads, Desktop, temp-extract, etc.)
VAULTS_FILE = Path.home() / ".doomvault_list.json"
SETTINGS_FILE = Path.home() / ".doomvault_settings.json"

def load_settings():
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"asset_mode": "reveal"}

def save_settings(s):
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[SETTINGS] ⚠ Could not save: {e}")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def find_free_port(start=7777):
    import socket
    for port in range(start, start + 100):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("No free port found")

def load_vaults():
    try:
        if VAULTS_FILE.exists():
            v = json.loads(VAULTS_FILE.read_text(encoding="utf-8")).get("vaults", [])
            print(f"[VAULTS] Loaded {len(v)} vault(s) from {VAULTS_FILE}")
            return v
        else:
            print(f"[VAULTS] No saved list yet at {VAULTS_FILE}")
    except Exception as e:
        print(f"[VAULTS] ⚠ Could not read vault list: {e}")
    return []

def save_vaults(vaults):
    try:
        VAULTS_FILE.write_text(json.dumps({"vaults": vaults}, indent=2), encoding="utf-8")
        print(f"[VAULTS] Saved {len(vaults)} vault(s) to {VAULTS_FILE}")
    except Exception as e:
        print(f"[VAULTS] ⚠ Could not save vault list to {VAULTS_FILE}: {e}")

# ─── GitHub API ───────────────────────────────────────────────────────────────

def gh_get(url, token=None):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def measure_download_size(url, token=None):
    """Return the Content-Length of a URL (following redirects) without
    downloading the body, or 0 if it can't be determined."""
    try:
        req = urllib.request.Request(url, method="GET")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        # Open but don't read the body — just inspect headers, then close.
        with urllib.request.urlopen(req, timeout=30) as resp:
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else 0
    except Exception:
        return 0

def gh_paginate(base_url, token=None, max_pages=200):
    results, page = [], 1
    truncated = False
    while page <= max_pages:
        url = base_url + ("&" if "?" in base_url else "?") + f"per_page=100&page={page}"
        try:
            batch = gh_get(url, token)
        except Exception:
            # Rate limit or network error partway through — keep what we have
            truncated = True
            break
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if page > max_pages:
        truncated = True
    # stash a flag callers can check without changing the return type for existing uses
    gh_paginate.last_truncated = truncated
    return results
gh_paginate.last_truncated = False

def download_file(url, dest_path, token=None, is_source_archive=False, on_bytes=None):
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists() and dest_path.stat().st_size > 0:
        # Already downloaded — count its size toward progress so the bar stays accurate
        if on_bytes:
            try: on_bytes(dest_path.stat().st_size)
            except Exception: pass
        return

    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    last_err = None
    for attempt in range(3):
        bytes_this_try = 0
        try:
            req = urllib.request.Request(url)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            if not is_source_archive:
                req.add_header("Accept", "application/octet-stream")
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as f:
                expected = resp.headers.get("Content-Length")
                expected = int(expected) if expected and expected.isdigit() else None
                while True:
                    chunk = resp.read(262144)   # 256 KB
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_this_try += len(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
            # Detect silent truncation: server closed early without erroring
            if expected is not None and bytes_this_try < expected:
                raise IOError(f"incomplete download: got {bytes_this_try} of {expected} bytes")
            # success — move temp file into place atomically
            os.replace(tmp_path, dest_path)
            return
        except Exception as e:
            last_err = e
            # roll back the progress counter for this failed attempt
            if on_bytes and bytes_this_try:
                try: on_bytes(-bytes_this_try)
                except Exception: pass
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            time.sleep(1.5 * (attempt + 1))   # back off and retry
    # all retries exhausted
    raise last_err if last_err else RuntimeError("download failed")

def run_git(args, cwd=None, capture=False):
    cmd = ["git"] + args
    if capture:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
        return (r.stdout or "").strip()
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)

def parse_repo(repo_arg):
    repo_arg = repo_arg.rstrip("/")
    if repo_arg.startswith("https://github.com/"):
        repo_arg = repo_arg[len("https://github.com/"):]
    if repo_arg.endswith(".git"):
        repo_arg = repo_arg[:-4]
    parts = [p for p in repo_arg.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Can't parse: '{repo_arg}'. Use owner/repo or full GitHub URL.")
    return parts[-2], parts[-1]

# ─── Archive worker ───────────────────────────────────────────────────────────

def run_archive(owner, repo, dest, token, no_snapshots, no_releases, commit_limit, log, release_limit=0, release_year=0):
    global cancel_requested, progress_state

    def prog(pct, step=""):
        progress_state["pct"]  = pct
        progress_state["step"] = step

    def chk():
        if cancel_requested:
            log("WARN", "Cancelled by user.")
            raise InterruptedError()

    try:
        dest = Path(dest).expanduser().resolve() / repo
        dest.mkdir(parents=True, exist_ok=True)
        repo_url     = f"https://github.com/{owner}/{repo}.git"
        repo_url_auth = repo_url.replace("https://", f"https://{token}@") if token else repo_url

        # 1 — Clone
        prog(2, "Cloning repository")
        log("STEP", "1/6 — Cloning repository")
        mirror  = dest / "_git_mirror"
        working = dest / "_working_copy"
        if mirror.exists():
            log("INFO", "Mirror exists — fetching updates...")
            run_git(["fetch", "--all", "--tags", "--prune"], cwd=mirror)
        else:
            log("INFO", f"Cloning {owner}/{repo}...")
            run_git(["clone", "--mirror", repo_url_auth, str(mirror)])
            run_git(["clone", str(mirror), str(working)])
        log("OK", "Repository cloned")
        chk()

        # 2 — Tags
        prog(8, "Collecting tags")
        log("STEP", "2/6 — Collecting tags")
        raw  = run_git(["tag", "--sort=-creatordate"], cwd=mirror, capture=True)
        tags = [t for t in raw.splitlines() if t.strip()]
        log("OK", f"Found {len(tags)} tags")

        # 3 — Snapshots
        if not no_snapshots and tags:
            log("STEP", "3/6 — Downloading snapshots")
            snap_dir = dest / "snapshots"
            snap_dir.mkdir(exist_ok=True)

            # Respect the same release filters for snapshots so we don't extract
            # every tag in history when the user only wants recent ones.
            snap_tags = tags
            if release_year and release_year > 0:
                kept = []
                for tag in tags:
                    # creator date of the tag (ISO), fall back to commit date
                    d = run_git(["log", "-1", "--format=%aI", f"refs/tags/{tag}"], cwd=mirror, capture=True)
                    if d[:4].isdigit() and int(d[:4]) >= release_year:
                        kept.append(tag)
                snap_tags = kept
                log("INFO", f"Snapshots since {release_year}: {len(snap_tags)} of {len(tags)} tags")
            if release_limit and release_limit > 0 and len(snap_tags) > release_limit:
                snap_tags = snap_tags[:release_limit]
                log("INFO", f"Limiting snapshots to {release_limit} most recent")

            for i, tag in enumerate(snap_tags):
                chk()
                prog(10 + int(20 * i / max(len(snap_tags), 1)), f"Snapshot {i+1}/{len(snap_tags)}")
                safe = re.sub(r"[^\w.\-]", "_", tag)
                out  = snap_dir / safe
                if out.exists():
                    log("SKIP", f"Snapshot exists: {tag}")
                    continue
                out.mkdir(parents=True, exist_ok=True)
                try:
                    tmp = str(dest / "_snap.tar")
                    run_git(["archive", "--format=tar", f"refs/tags/{tag}", f"--output={tmp}"], cwd=mirror)
                    with tarfile.open(tmp) as t:
                        t.extractall(out)
                    os.unlink(tmp)
                    log("OK", f"Snapshot: {tag} ({i+1}/{len(snap_tags)})")
                except Exception as e:
                    log("WARN", f"Snapshot failed for {tag}: {e}")
                    shutil.rmtree(out, ignore_errors=True)
        else:
            log("SKIP", "3/6 — Skipping snapshots")

        # 4 — Releases
        releases = []
        if not no_releases:
            log("STEP", "4/6 — Fetching GitHub releases")
            try:
                raw_rels    = gh_paginate(f"https://api.github.com/repos/{owner}/{repo}/releases", token)
                # Filter by year first (releases published in release_year or later)
                if release_year and release_year > 0:
                    before = len(raw_rels)
                    raw_rels = [r for r in raw_rels
                                if (r.get("published_at") or r.get("created_at") or "")[:4].isdigit()
                                and int((r.get("published_at") or r.get("created_at"))[:4]) >= release_year]
                    log("INFO", f"Releases since {release_year}: kept {len(raw_rels)} of {before}")
                # GitHub returns releases newest-first; keep only the N most recent if limited
                if release_limit and release_limit > 0 and len(raw_rels) > release_limit:
                    log("INFO", f"Limiting to {release_limit} most recent releases (of {len(raw_rels)})")
                    raw_rels = raw_rels[:release_limit]
                releases_dir = dest / "releases"
                releases_dir.mkdir(exist_ok=True)

                # ── Pre-compute total download size for an accurate ETA ──
                total_bytes = 0
                for rel in raw_rels:
                    for asset in rel.get("assets", []):
                        total_bytes += asset.get("size", 0) or 0
                    # source archives report no size via API; estimate ~5 MB each
                    if rel.get("zipball_url"): total_bytes += 5_000_000
                    if rel.get("tarball_url"): total_bytes += 5_000_000
                progress_state["bytes_total"] = total_bytes
                progress_state["bytes_done"]  = 0
                progress_state["dl_start"]    = time.time()
                log("INFO", f"Estimated download size: {total_bytes/1048576:.0f} MB across {len(raw_rels)} releases")

                def add_bytes(n):
                    progress_state["bytes_done"] += n
                for ri, rel in enumerate(raw_rels):
                    chk()
                    prog(30 + int(50 * ri / max(len(raw_rels), 1)), f"Release {ri+1}/{len(raw_rels)}")
                    tag      = rel["tag_name"]
                    safe_tag = re.sub(r"[^\w.\-]", "_", tag)
                    rel_dir  = releases_dir / safe_tag
                    rel_dir.mkdir(exist_ok=True)
                    log("INFO", f"Release: {tag} ({ri+1}/{len(raw_rels)})")
                    meta = {
                        "id": rel["id"], "tag_name": tag,
                        "name": rel.get("name") or tag,
                        "prerelease": rel["prerelease"], "draft": rel["draft"],
                        "published_at": rel.get("published_at", ""),
                        "author": rel.get("author", {}).get("login", "unknown"),
                        "body": rel.get("body") or "",
                        "html_url": rel.get("html_url", ""), "assets": []
                    }
                    for asset in rel.get("assets", []):
                        adest = rel_dir / asset["name"]
                        try:
                            download_file(asset["browser_download_url"], adest, token, on_bytes=add_bytes)
                            log("OK", f"  ↳ {asset['name']}")
                        except Exception as e:
                            log("WARN", f"  ↳ Failed: {asset['name']}: {e}")
                        meta["assets"].append({
                            "name": asset["name"], "size": asset["size"],
                            "download_count": asset.get("download_count", 0),
                            "content_type": asset.get("content_type", ""),
                            "local_path": str(adest.relative_to(dest))
                        })
                    for stype, surl, sfname in [
                        ("zip", rel.get("zipball_url"), f"source-{safe_tag}.zip"),
                        ("tar", rel.get("tarball_url"), f"source-{safe_tag}.tar.gz"),
                    ]:
                        if surl:
                            try:
                                download_file(surl, rel_dir / sfname, token, is_source_archive=True, on_bytes=add_bytes)
                                sz = (rel_dir / sfname).stat().st_size if (rel_dir / sfname).exists() else 0
                                meta["assets"].append({
                                    "name": sfname, "size": sz, "download_count": 0,
                                    "content_type": "application/zip",
                                    "local_path": str((rel_dir / sfname).relative_to(dest))
                                })
                            except Exception as e:
                                log("WARN", f"  ↳ Source archive failed: {e}")
                    with open(rel_dir / "release.json", "w") as f:
                        json.dump(meta, f, indent=2)
                    releases.append(meta)
                with open(releases_dir / "index.json", "w") as f:
                    json.dump(releases, f, indent=2)
                log("OK", f"Archived {len(releases)} releases")
            except Exception as e:
                log("WARN", f"Releases failed: {e}")
        else:
            log("SKIP", "4/6 — Skipping releases")
            idx = dest / "releases" / "index.json"
            if idx.exists():
                releases = json.loads(idx.read_text())
        chk()

        # 5 — Commits
        prog(82, "Reading commit log")
        log("STEP", "5/6 — Reading commit log")
        fmt     = "%H%x00%h%x00%an%x00%ae%x00%ai%x00%s"
        raw_log = run_git(["log", f"--pretty=format:{fmt}", f"-n{commit_limit}", "HEAD"], cwd=mirror, capture=True)
        commits = []
        for line in raw_log.splitlines():
            parts = line.split("\x00")
            if len(parts) == 6:
                commits.append({"hash": parts[0], "short": parts[1], "author": parts[2],
                                 "email": parts[3], "date": parts[4], "subject": parts[5]})
        log("OK", f"Collected {len(commits)} commits")

        # 6 — HTML
        prog(92, "Generating browser")
        log("STEP", "6/6 — Generating offline browser")
        html_path = generate_html(dest, owner, repo, releases, tags, commits)
        manifest  = {
            "archived_at": datetime.now().isoformat(), "owner": owner, "repo": repo,
            "tags": len(tags), "releases": len(releases), "commits": len(commits),
        }
        with open(dest / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)
        prog(100, "Complete")
        log("DONE", str(html_path))

    except InterruptedError:
        pass
    except Exception as e:
        log("ERROR", str(e))

# ─── Offline archive HTML (written to disk) ───────────────────────────────────

BROWSER_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{REPO_FULL}} — DoomVault</title>
<style>
  :root{--bg:#0a0a0f;--surface:#111118;--border:#1e1e2e;--accent:#00ff9d;--accent2:#7c6aff;--warn:#ff6b35;--text:#e2e8f0;--muted:#64748b;--tag-bg:#1a1a2e;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:'Cascadia Code','Fira Code','Consolas','Menlo',monospace;background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column;}
  header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 2rem;display:flex;align-items:center;gap:2rem;height:60px;position:sticky;top:0;z-index:100;}
  .logo{font-family:'Trebuchet MS',system-ui,sans-serif;font-weight:800;font-size:1.1rem;color:var(--accent);}
  .repo-title{font-size:.85rem;color:var(--muted);flex:1;}
  .repo-title span{color:var(--text);}
  .badge-hdr{font-size:.7rem;color:var(--accent);border:1px solid var(--accent);padding:2px 8px;border-radius:99px;}
  .layout{display:flex;flex:1;overflow:hidden;height:calc(100vh - 60px);}
  nav{width:200px;min-width:200px;background:var(--surface);border-right:1px solid var(--border);overflow-y:auto;padding:1rem 0;}
  .nav-s{padding:.5rem 1.2rem .25rem;font-size:.6rem;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);}
  .nav-btn{display:flex;align-items:center;gap:.6rem;padding:.5rem 1.2rem;cursor:pointer;font-family:inherit;font-size:.78rem;color:var(--muted);border:none;background:none;width:100%;text-align:left;border-left:2px solid transparent;transition:all .15s;}
  .nav-btn:hover{color:var(--text);background:#ffffff08;}
  .nav-btn.active{color:var(--accent);border-left-color:var(--accent);background:#00ff9d10;}
  .nav-count{margin-left:auto;font-size:.65rem;background:#1a1a2e;padding:1px 6px;border-radius:99px;color:var(--muted);}
  main{flex:1;overflow-y:auto;padding:2rem;}
  .panel{display:none;} .panel.active{display:block;}
  .hero{text-align:center;padding:3rem 1rem 2rem;max-width:700px;margin:0 auto;}
  .hero h1{font-family:'Trebuchet MS',system-ui,sans-serif;font-size:2.5rem;font-weight:800;color:var(--accent);margin-bottom:.5rem;}
  .hero p{color:var(--muted);font-size:.85rem;}
  .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1rem;margin-top:2rem;}
  .stat-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem;text-align:center;}
  .stat-num{font-family:'Trebuchet MS',system-ui,sans-serif;font-size:2rem;font-weight:700;color:var(--accent);}
  .stat-lbl{font-size:.72rem;color:var(--muted);margin-top:.25rem;}
  .panel-title{font-family:'Trebuchet MS',system-ui,sans-serif;font-size:1.3rem;font-weight:700;margin-bottom:1.5rem;color:var(--text);display:flex;align-items:center;gap:.75rem;}
  .panel-title::after{content:'';flex:1;height:1px;background:var(--border);}
  .releases-list{display:flex;flex-direction:column;gap:1.2rem;}
  .rel-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;}
  .rel-card:hover{border-color:var(--accent2);}
  .rel-head{display:flex;align-items:flex-start;gap:1rem;padding:1.2rem 1.5rem;cursor:pointer;}
  .rel-head-left{flex:1;}
  .rel-tag{font-family:'Trebuchet MS',system-ui,sans-serif;font-size:1.1rem;font-weight:700;color:var(--text);display:flex;align-items:center;gap:.5rem;}
  .badge{font-size:.6rem;padding:2px 7px;border-radius:99px;font-weight:600;text-transform:uppercase;}
  .badge-pre{background:#ff6b3520;color:var(--warn);border:1px solid var(--warn);}
  .badge-stable{background:#00ff9d15;color:var(--accent);border:1px solid var(--accent);}
  .rel-name{font-size:.82rem;color:var(--muted);margin-top:.2rem;}
  .rel-meta{font-size:.72rem;color:var(--muted);margin-top:.35rem;display:flex;gap:1rem;flex-wrap:wrap;}
  .rel-chev{font-size:1.1rem;color:var(--muted);transition:transform .2s;}
  .rel-card.open .rel-chev{transform:rotate(180deg);}
  .rel-body{display:none;border-top:1px solid var(--border);}
  .rel-card.open .rel-body{display:block;}
  .rel-notes{padding:1.2rem 1.5rem;font-size:.8rem;line-height:1.8;white-space:pre-wrap;max-height:400px;overflow-y:auto;color:var(--text);border-bottom:1px solid var(--border);}
  .rel-notes:empty::before{content:"No release notes.";color:var(--muted);font-style:italic;}
  .rel-assets{padding:1rem 1.5rem;}
  .rel-assets h4{font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:.6rem;}
  .assets-grid{display:flex;flex-wrap:wrap;gap:.5rem;}
  .asset-chip{display:inline-flex;align-items:center;gap:.35rem;background:#0a0a0f;border:1px solid var(--border);border-radius:8px;padding:.4rem .7rem;font-size:.72rem;color:var(--text);text-decoration:none;transition:all .15s;}
  .asset-chip:hover{border-color:var(--accent);color:var(--accent);}
  .asset-size{color:var(--muted);font-size:.65rem;}
  .search-bar{margin-bottom:1.2rem;}
  .search-bar input{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.6rem 1rem;color:var(--text);font-family:inherit;font-size:.8rem;outline:none;}
  .search-bar input:focus{border-color:var(--accent2);}
  .commit-table{width:100%;border-collapse:collapse;font-size:.78rem;}
  .commit-table th{text-align:left;padding:.6rem .8rem;color:var(--muted);font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid var(--border);font-weight:400;}
  .commit-table td{padding:.6rem .8rem;border-bottom:1px solid #ffffff08;vertical-align:top;}
  .commit-table tr:hover td{background:#ffffff04;}
  .c-hash{color:var(--accent2);white-space:nowrap;}
  .c-author{color:var(--muted);white-space:nowrap;}
  .c-date{color:var(--muted);font-size:.7rem;white-space:nowrap;}
  .tags-grid{display:flex;flex-wrap:wrap;gap:.75rem;}
  .tag-pill{display:inline-flex;align-items:center;gap:.5rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.5rem 1rem;font-size:.8rem;color:var(--text);}
  .empty{text-align:center;padding:4rem;color:var(--muted);}
  .empty span{font-size:3rem;display:block;margin-bottom:1rem;}
  ::-webkit-scrollbar{width:4px;} ::-webkit-scrollbar-track{background:transparent;} ::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px;}
</style></head><body>
<header>
  <div class="logo">☢ DOOMVAULT</div>
  <div class="repo-title"><span>{{REPO_FULL}}</span></div>
  <div class="badge-hdr">OFFLINE ARCHIVE</div>
</header>
<div class="layout">
  <nav>
    <div class="nav-s">Overview</div>
    <button class="nav-btn active" onclick="show('overview',this)">◈ Summary</button>
    <div class="nav-s">History</div>
    <button class="nav-btn" onclick="show('releases',this)">🏷 Releases<span class="nav-count" id="cnt-r">0</span></button>
    <button class="nav-btn" onclick="show('commits',this)">◎ Commits<span class="nav-count" id="cnt-c">0</span></button>
    <button class="nav-btn" onclick="show('tags',this)">◇ Tags<span class="nav-count" id="cnt-t">0</span></button>
  </nav>
  <main>
    <div class="panel active" id="panel-overview">
      <div class="hero"><h1>{{REPO_NAME}}</h1><p>{{REPO_FULL}} · Archived {{ARCHIVE_DATE}}</p><div class="stats-grid" id="stats"></div></div>
    </div>
    <div class="panel" id="panel-releases">
      <div class="panel-title">🏷 Release History</div>
      <div class="releases-list" id="rel-list"><div class="empty"><span>📭</span>No releases.</div></div>
    </div>
    <div class="panel" id="panel-commits">
      <div class="panel-title">◎ Commit Log</div>
      <div class="search-bar"><input type="text" id="cq" placeholder="Filter by message, author, hash…" oninput="filterCommits()"></div>
      <table class="commit-table"><thead><tr><th>Hash</th><th>Subject</th><th>Author</th><th>Date</th></tr></thead><tbody id="ctb"></tbody></table>
    </div>
    <div class="panel" id="panel-tags">
      <div class="panel-title">◇ All Tags</div>
      <div class="tags-grid" id="tags-grid"></div>
    </div>
  </main>
</div>
<script>
const D={{JSON_DATA}};
document.getElementById('cnt-r').textContent=D.releases.length;
document.getElementById('cnt-c').textContent=D.commits.length;
document.getElementById('cnt-t').textContent=D.tags.length;
const stable=D.releases.filter(r=>!r.prerelease&&!r.draft).length;
document.getElementById('stats').innerHTML=[
  {n:D.releases.length,l:'Total Releases'},{n:stable,l:'Stable'},{n:D.releases.filter(r=>r.prerelease).length,l:'Pre-releases'},
  {n:D.commits.length,l:'Commits'},{n:D.tags.length,l:'Tags'}
].map(c=>`<div class="stat-card"><div class="stat-num">${c.n}</div><div class="stat-lbl">${c.l}</div></div>`).join('');
document.getElementById('rel-list').innerHTML=D.releases.length?D.releases.map((r,i)=>{
  const b=r.prerelease?'<span class="badge badge-pre">pre</span>':'<span class="badge badge-stable">stable</span>';
  const d=r.published_at?new Date(r.published_at).toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'}):'';
  const a=(r.assets||[]).map(a=>`<a class="asset-chip" href="${a.local_path}" download>📦 ${x(a.name)}<span class="asset-size">${fmt(a.size)}</span></a>`).join('');
  return `<div class="rel-card" id="rc${i}"><div class="rel-head" onclick="document.getElementById('rc${i}').classList.toggle('open')">
    <div class="rel-head-left"><div class="rel-tag">${x(r.tag_name)}${b}</div>${r.name&&r.name!==r.tag_name?`<div class="rel-name">${x(r.name)}</div>`:''}
    <div class="rel-meta">${r.author?`<span>👤${x(r.author)}</span>`:''}${d?`<span>📅${d}</span>`:''}<span>📎${(r.assets||[]).length} assets</span></div></div>
    <span class="rel-chev">⌄</span></div>
    <div class="rel-body"><div class="rel-notes">${x(r.body||'')}</div>${a?`<div class="rel-assets"><h4>Assets</h4><div class="assets-grid">${a}</div></div>`:''}</div></div>`;
}).join(''):'<div class="empty"><span>📭</span>No releases.</div>';
let allC=D.commits;
function renderC(list){document.getElementById('ctb').innerHTML=list.length?list.map(c=>`<tr><td><span class="c-hash">${x(c.short)}</span></td><td>${x(c.subject)}</td><td class="c-author">${x(c.author)}</td><td class="c-date">${c.date?c.date.slice(0,10):''}</td></tr>`).join(''):'<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:2rem">No results</td></tr>';}
renderC(allC);
function filterCommits(){const q=document.getElementById('cq').value.toLowerCase();renderC(q?allC.filter(c=>c.subject.toLowerCase().includes(q)||c.author.toLowerCase().includes(q)||c.short.toLowerCase().includes(q)):allC);}
document.getElementById('tags-grid').innerHTML=D.tags.length?D.tags.map(t=>`<div class="tag-pill">🏷 ${x(t)}</div>`).join(''):'<div class="empty"><span>🏷</span>No tags.</div>';
function show(p,btn){document.querySelectorAll('.panel').forEach(e=>e.classList.remove('active'));document.querySelectorAll('.nav-btn').forEach(e=>e.classList.remove('active'));document.getElementById('panel-'+p).classList.add('active');btn.classList.add('active');}
function x(s){if(!s)return'';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmt(b){if(!b)return'?';if(b<1024)return b+'B';if(b<1048576)return(b/1024).toFixed(1)+'KB';return(b/1048576).toFixed(1)+'MB';}
</script></body></html>"""

def generate_html(dest, owner, repo, releases, tags, commits):
    data = {"owner": owner, "repo": repo, "releases": releases, "tags": tags, "commits": commits}
    html = BROWSER_HTML
    html = html.replace("{{REPO_FULL}}", f"{owner}/{repo}")
    html = html.replace("{{REPO_NAME}}", repo)
    html = html.replace("{{ARCHIVE_DATE}}", datetime.now().strftime("%Y-%m-%d"))
    html = html.replace("{{JSON_DATA}}", json.dumps(data))
    out  = Path(dest) / "index.html"
    out.write_text(html, encoding="utf-8")
    return out

# ─── App UI HTML (served by local server) ─────────────────────────────────────

UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DoomVault</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:      #0a0a0f;
  --surface: #111118;
  --border:  #1e1e2e;
  --accent:  #00ff9d;
  --accent2: #7c6aff;
  --warn:    #ff6b35;
  --text:    #e2e8f0;
  --muted:   #64748b;
  --mono:    'Cascadia Code','Fira Code','Consolas','Menlo',monospace;
  --head:    'Trebuchet MS','Gill Sans',system-ui,sans-serif;
}

html, body {
  height: 100%;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 14px;
}

/* ── Shell ── */
#shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

#topbar {
  display: flex;
  align-items: center;
  gap: 1.5rem;
  padding: 0 1.5rem;
  height: 52px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}

.logo { font-family: var(--head); font-weight: 800; font-size: 1.05rem; color: var(--accent); }

.tabs { display: flex; gap: 4px; }
.tab-btn {
  padding: 6px 16px; border-radius: 6px; border: none; background: none;
  font-family: var(--mono); font-size: .78rem; color: var(--muted); cursor: pointer;
  transition: all .15s;
}
.tab-btn:hover { color: var(--text); background: #ffffff0a; }
.tab-btn.on    { color: var(--accent); background: #00ff9d12; }

#body {
  flex: 1;
  min-height: 0;
  position: relative;
}

.view {
  position: absolute;
  inset: 0;
  overflow-y: auto;
  padding: 2rem;
  display: none;
}
.view.on { display: block; }

/* ── Vault tab ── */
.vault-center { max-width: 520px; margin: 0 auto; }
.v-hero { text-align: center; padding: 1.5rem 0 1.75rem; }
.v-hero .skull { font-size: 2.8rem; color: var(--accent); filter: drop-shadow(0 0 16px #00ff9d66); }
.v-hero h1 { font-family: var(--head); font-size: 1.9rem; font-weight: 800; color: var(--accent); margin-top: .25rem; }
.v-hero p  { color: var(--muted); font-size: .8rem; margin-top: .2rem; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.5rem; }
.field { margin-bottom: 1rem; }
.field:last-child { margin-bottom: 0; }

label, .lbl {
  display: block; font-size: 1.2rem; text-transform: uppercase;
  letter-spacing: .08em; color: #94a3b8; font-weight: 800; margin-bottom: .5rem;
}
.lbl-row { display: flex; align-items: center; gap: .5rem; margin-bottom: .4rem; }
.lbl-row label { margin-bottom: 0; }

input[type=text] {
  width: 100%; background: var(--bg); border: 1px solid var(--border);
  border-radius: 8px; padding: .6rem .85rem; color: var(--text);
  font-family: var(--mono); font-size: .82rem; outline: none; transition: border-color .15s;
}
input[type=text]:focus { border-color: var(--accent2); }

.row2 { display: flex; gap: .5rem; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.hint { color: var(--muted); font-size: .62rem; text-transform: none; font-weight: 400; }
.hint-block { display: block; margin: -.25rem 0 .45rem; }
.sub-label { font-size: .82rem; font-weight: 700; color: var(--text); margin-bottom: .35rem; }
.row2 input { flex: 1; }

.ghost-btn {
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  padding: .6rem .85rem; color: var(--muted); font-family: var(--mono);
  font-size: .75rem; cursor: pointer; white-space: nowrap; transition: all .15s;
}
.ghost-btn:hover { border-color: var(--accent); color: var(--accent); }
.ghost-btn.v2:hover { border-color: var(--accent2); color: var(--accent2); }

.toggles { display: flex; flex-wrap: wrap; gap: .5rem; }
.tog {
  display: inline-flex; align-items: center; gap: .4rem;
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  padding: .4rem .8rem; font-family: var(--mono); font-size: .72rem;
  color: var(--muted); cursor: pointer; transition: all .15s; user-select: none;
}
.tog.on { border-color: var(--accent); color: var(--accent); background: #00ff9d08; }

.info-dot {
  width: 15px; height: 15px; border-radius: 50%; border: 1px solid var(--muted);
  background: transparent; color: var(--muted); font-size: .6rem; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0; transition: all .15s;
}
.info-dot:hover { border-color: var(--accent); color: var(--accent); }

.action-row { display: flex; gap: .6rem; margin-top: 1.25rem; }
.vault-btn {
  flex: 3; padding: .75rem; background: var(--accent); color: #0a0a0f;
  border: none; border-radius: 8px; font-family: var(--head); font-size: .95rem;
  font-weight: 800; cursor: pointer; letter-spacing: .04em; transition: opacity .15s;
}
.vault-btn:hover:not(:disabled) { opacity: .88; }
.vault-btn:disabled { opacity: .4; cursor: not-allowed; }
.estimate-btn {
  flex: 1; padding: .75rem .5rem; background: #035efc; color: #fff;
  border: none; border-radius: 8px; font-family: var(--head); font-size: .72rem;
  font-weight: 700; cursor: pointer; transition: opacity .15s; white-space: nowrap;
}
.estimate-btn:hover:not(:disabled) { opacity: .88; }
.estimate-btn:disabled { opacity: .5; cursor: not-allowed; }
.cancel-btn {
  display: none; padding: .75rem 1rem; background: transparent;
  color: var(--warn); border: 1px solid var(--warn); border-radius: 8px;
  font-family: var(--mono); font-size: .82rem; font-weight: 700;
  cursor: pointer; transition: background .15s;
}
.cancel-btn:hover { background: #ff6b3514; }
#estimate-result { margin-top: .85rem; }
.est-box {
  background: var(--surface); border: 1px solid #035efc; border-radius: 10px;
  padding: 1rem 1.1rem; font-size: .8rem; color: var(--text);
}
.est-box .est-total { font-family: var(--head); font-size: 1.3rem; font-weight: 800; color: #035efc; }
.est-box .est-line { display: flex; justify-content: space-between; padding: .25rem 0; color: var(--muted); font-size: .74rem; }
.est-box .est-line span:last-child { color: var(--text); }
.est-box .est-note { font-size: .66rem; color: var(--muted); margin-top: .5rem; line-height: 1.5; }

/* progress */
#prog-wrap { margin-top: 1.25rem; display: none; }
#prog-bar-shell { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: .85rem 1rem; margin-bottom: .6rem; }
.pb-top { display: flex; justify-content: space-between; font-size: .72rem; margin-bottom: .5rem; }
.pb-step { color: var(--text); }
.pb-pct  { color: var(--accent); font-weight: 700; }
.pb-eta  { color: var(--muted); font-size: .68rem; }
.pb-track { height: 5px; background: var(--border); border-radius: 99px; overflow: hidden; }
.pb-fill  { height: 100%; background: var(--accent); width: 0%; transition: width .4s ease; border-radius: 99px; }
#log-box {
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: .85rem 1rem; height: 190px; overflow-y: auto; font-size: .72rem; line-height: 1.8;
}
.l-STEP { color: var(--accent2); font-weight: 600; }
.l-OK   { color: var(--accent); }
.l-WARN { color: var(--warn); }
.l-ERROR{ color: #f44; font-weight: 600; }
.l-INFO { color: var(--muted); }
.l-SKIP { color: #444; }
.l-DONE { color: var(--accent); font-weight: 700; }
.vault-done-msg {
  display: none; width: 100%; margin-top: .7rem; padding: .7rem;
  background: #00ff9d12; color: var(--accent); border: 1px solid var(--accent);
  border-radius: 8px; font-size: .82rem; text-align: center; cursor: pointer;
  transition: background .15s;
}
.vault-done-msg:hover { background: #00ff9d20; }

/* ── Explore tab ── */
#explore-shell {
  position: absolute; inset: 0;
  display: none;
  flex-direction: row;
}
#explore-shell.on { display: flex; }

#vault-sidebar {
  width: 210px; min-width: 210px; flex-shrink: 0;
  background: var(--surface); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; overflow: hidden;
}
.sidebar-hdr {
  display: flex; align-items: center; justify-content: space-between;
  padding: .75rem 1rem; font-size: .62rem; text-transform: uppercase;
  letter-spacing: .1em; color: var(--muted); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.add-btn {
  background: none; border: 1px solid var(--border); border-radius: 5px;
  color: var(--muted); font-size: .7rem; padding: 2px 8px; cursor: pointer;
  font-family: var(--mono); transition: all .15s;
}
.add-btn:hover { border-color: var(--accent); color: var(--accent); }

#vault-list { flex: 1; overflow-y: auto; }
.v-item {
  padding: .6rem 1rem; cursor: pointer; border-left: 2px solid transparent;
  transition: all .15s; display: flex; flex-direction: column; gap: .1rem;
}
.v-item:hover { background: #ffffff06; }
.v-item.on    { border-left-color: var(--accent); background: #00ff9d07; }
.v-item .vi-name { font-size: .75rem; font-weight: 600; color: var(--muted); }
.v-item.on .vi-name { color: var(--accent); }
.v-item .vi-meta { font-size: .62rem; color: #444; }
.v-empty { padding: 1.5rem 1rem; font-size: .72rem; color: var(--muted); text-align: center; line-height: 1.6; }

#vault-detail {
  flex: 1; min-width: 0; display: flex; flex-direction: column; overflow: hidden;
}
#detail-empty { margin: auto; text-align: center; color: var(--muted); font-size: .82rem; padding: 2rem; }

#detail-tabs {
  display: flex; align-items: center; gap: .75rem;
  border-bottom: 1px solid var(--border);
  background: var(--surface); flex-shrink: 0; padding: 0 1.25rem;
}
.dtab-group { display: flex; }
.dtab {
  padding: .6rem 1rem; font-size: .75rem; color: var(--muted); cursor: pointer;
  border: none; background: none; font-family: var(--mono);
  border-bottom: 2px solid transparent; transition: all .15s;
  user-select: none;
}
.dtab:hover { color: var(--text); }
.dtab.on    { color: var(--accent); border-bottom-color: var(--accent); }
.dtab.dragging { opacity: .4; }
.dl-updates-btn {
  display: inline-flex; align-items: center; gap: .4rem;
  background: #ffd23f; border: none; border-radius: 7px; padding: .4rem .9rem;
  color: #1a1505; font-family: var(--head); font-size: .76rem; font-weight: 800;
  cursor: pointer; transition: background .15s, opacity .15s; white-space: nowrap; margin: .35rem 0;
}
.dl-updates-btn:hover { opacity: .85; }
.dl-updates-btn:disabled { opacity: .6; cursor: default; }
.dl-updates-btn .dl-arrow { flex-shrink: 0; display: block; }
.dl-updates-btn.uptodate { background: var(--accent); color: #0a0a0f; }

/* README rendered like GitHub */
.readme-wrap { max-width: 900px; }

#detail-body { flex: 1; overflow-y: auto; padding: 1.25rem 1.5rem; }

/* releases pager */
.d-releases { display: flex; flex-direction: column; gap: .85rem; }
.d-rel-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.d-rel-head { display: flex; align-items: flex-start; gap: .75rem; padding: .9rem 1.1rem; cursor: pointer; }
.d-rel-head-left { flex: 1; }
.d-tag-line { display: flex; align-items: center; gap: .4rem; flex-wrap: wrap; }
.d-tag { font-family: var(--head); font-size: .95rem; font-weight: 700; color: var(--text); }
.d-badge { font-size: .58rem; padding: 2px 6px; border-radius: 99px; font-weight: 600; text-transform: uppercase; }
.d-pre    { background: #ff6b3520; color: var(--warn); border: 1px solid var(--warn); }
.d-stable { background: #00ff9d15; color: var(--accent); border: 1px solid var(--accent); }
.d-rel-meta { font-size: .67rem; color: var(--muted); margin-top: .3rem; display: flex; gap: .6rem; flex-wrap: wrap; }
.d-chev { color: var(--muted); font-size: .9rem; transition: transform .2s; flex-shrink: 0; margin-top: 2px; }
.d-rel-card.open .d-chev { transform: rotate(180deg); }
.d-rel-body { display: none; border-top: 1px solid var(--border); }
.d-rel-card.open .d-rel-body { display: block; }
.d-notes { padding: 1.1rem 1.25rem; font-size: .82rem; line-height: 1.65; max-height: 460px; overflow-y: auto; color: var(--text); border-bottom: 1px solid var(--border); }
.d-notes-empty { padding: 1.1rem 1.25rem; color: var(--muted); font-style: italic; font-size: .8rem; border-bottom: 1px solid var(--border); }
/* GitHub-style markdown rendering */
.md h1 { font-family: var(--head); font-size: 1.35rem; font-weight: 800; margin: 1.2rem 0 .7rem; padding-bottom: .3rem; border-bottom: 1px solid var(--border); color: var(--text); }
.md h2 { font-family: var(--head); font-size: 1.15rem; font-weight: 700; margin: 1.1rem 0 .6rem; padding-bottom: .25rem; border-bottom: 1px solid var(--border); color: var(--text); }
.md h3 { font-family: var(--head); font-size: 1rem; font-weight: 700; margin: 1rem 0 .5rem; color: var(--text); }
.md h4 { font-size: .9rem; font-weight: 700; margin: .9rem 0 .4rem; color: var(--text); }
.md h1:first-child, .md h2:first-child, .md h3:first-child { margin-top: 0; }
.md p { margin: .55rem 0; }
.md ul, .md ol { margin: .55rem 0; padding-left: 1.6rem; }
.md li { margin: .25rem 0; }
.md a { color: var(--accent2); text-decoration: none; }
.md a:hover { text-decoration: underline; }
.md code { background: #ffffff10; border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; font-family: var(--mono); font-size: .78em; color: var(--accent); }
.md pre { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: .8rem 1rem; overflow-x: auto; margin: .7rem 0; }
.md pre code { background: none; border: none; padding: 0; color: var(--text); font-size: .76rem; }
.md blockquote { border-left: 3px solid var(--border); padding-left: 1rem; margin: .7rem 0; color: var(--muted); }
.md hr { border: none; border-top: 1px solid var(--border); margin: 1rem 0; }
.md strong { color: var(--text); font-weight: 700; }
.md em { font-style: italic; }
.md img { max-width: 100%; border-radius: 6px; }
.md table { border-collapse: collapse; margin: .7rem 0; font-size: .78rem; }
.md th, .md td { border: 1px solid var(--border); padding: .35rem .65rem; }
.md th { background: #ffffff08; font-weight: 700; }

.d-assets { padding: .85rem 1.25rem; }
.d-assets h4 { font-size: .65rem; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin-bottom: .6rem; }
.d-asset-list { display: flex; flex-direction: column; }
.d-asset-row { display: flex; align-items: center; gap: .6rem; padding: .6rem .25rem; border-bottom: 1px solid var(--border); font-size: .8rem; color: var(--text); text-decoration: none; transition: background .12s; cursor: pointer; }
.d-asset-row:last-child { border-bottom: none; }
.d-asset-row:hover { background: #ffffff06; }
.d-asset-row:hover .d-asset-name { color: var(--accent); }
.d-asset-icon { flex-shrink: 0; opacity: .7; }
.d-asset-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: color .12s; }
.d-asset-sz { color: var(--muted); font-size: .72rem; flex-shrink: 0; }

.pager { display: flex; align-items: center; justify-content: center; gap: .75rem; padding: .75rem 0 0; font-size: .75rem; color: var(--muted); }
.pg-btn { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: .3rem .75rem; color: var(--muted); font-family: var(--mono); font-size: .72rem; cursor: pointer; transition: all .15s; }
.pg-btn:hover:not(:disabled) { border-color: var(--accent2); color: var(--accent2); }
.pg-btn:disabled { opacity: .3; cursor: default; }

/* commits */
.c-search { width: 100%; margin-bottom: .85rem; }
.c-search input { width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: .55rem .85rem; color: var(--text); font-family: var(--mono); font-size: .78rem; outline: none; transition: border-color .15s; }
.c-search input:focus { border-color: var(--accent2); }
.c-table { width: 100%; border-collapse: collapse; font-size: .74rem; }
.c-table th { text-align: left; padding: .5rem .65rem; color: var(--muted); font-size: .62rem; text-transform: uppercase; letter-spacing: .08em; border-bottom: 1px solid var(--border); font-weight: 400; }
.c-table td { padding: .5rem .65rem; border-bottom: 1px solid #ffffff06; vertical-align: top; }
.c-table tr:hover td { background: #ffffff04; }
.c-hash   { color: var(--accent2); white-space: nowrap; font-size: .7rem; }
.c-author { color: var(--muted); white-space: nowrap; font-size: .7rem; }
.c-date   { color: var(--muted); white-space: nowrap; font-size: .67rem; }
.c-row { cursor: pointer; }
.c-row:hover td { background: #ffffff08 !important; }

/* commit detail */
.cd-topbar { display: flex; align-items: center; justify-content: space-between; gap: .75rem; margin-bottom: 1rem; flex-wrap: wrap; }
.back-btn { background: var(--bg); border: 1px solid var(--border); border-radius: 7px; padding: .4rem .9rem; color: var(--accent2); font-family: var(--mono); font-size: .75rem; cursor: pointer; transition: all .15s; }
.back-btn:hover { border-color: var(--accent2); background: #7c6aff12; }
.export-btn { background: var(--accent); border: none; border-radius: 7px; padding: .45rem 1rem; color: #0a0a0f; font-family: var(--head); font-size: .78rem; font-weight: 700; cursor: pointer; transition: opacity .15s; }
.export-btn:hover { opacity: .88; }
.c-export-btn { background: var(--accent); border: none; border-radius: 5px; padding: 3px 9px; color: #0a0a0f; font-family: var(--head); font-size: .66rem; font-weight: 700; white-space: nowrap; cursor: pointer; transition: opacity .15s; line-height: 1.3; }
.c-export-btn:hover { opacity: .88; }
.commit-detail { }
.cd-subject { font-family: var(--head); font-size: 1.1rem; font-weight: 700; color: var(--text); margin-bottom: .5rem; }
.cd-body { font-size: .8rem; line-height: 1.7; color: var(--muted); white-space: pre-wrap; margin-bottom: .85rem; padding: .7rem .9rem; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
.cd-meta { display: flex; gap: 1rem; flex-wrap: wrap; font-size: .72rem; color: var(--muted); padding-bottom: .85rem; border-bottom: 1px solid var(--border); margin-bottom: .85rem; }
.cd-files { display: flex; flex-direction: column; gap: .2rem; margin-bottom: 1rem; }
.fstat-row { display: flex; align-items: center; gap: .6rem; font-size: .76rem; padding: .25rem 0; }
.fstat-badge { font-size: .58rem; padding: 1px 6px; border-radius: 4px; font-weight: 700; text-transform: uppercase; flex-shrink: 0; width: 62px; text-align: center; }
.fstat-add { background: #00ff9d18; color: var(--accent); border: 1px solid var(--accent); }
.fstat-del { background: #ff6b3518; color: var(--warn); border: 1px solid var(--warn); }
.fstat-mod { background: #7c6aff18; color: var(--accent2); border: 1px solid var(--accent2); }
.fstat-name { color: var(--text); font-family: var(--mono); word-break: break-all; }
.cd-diff { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; overflow-x: auto; max-height: 520px; overflow-y: auto; }
.cd-diff pre { font-family: var(--mono); font-size: .72rem; line-height: 1.5; padding: .8rem 1rem; white-space: pre; }
.dl-add  { color: var(--accent); background: #00ff9d0c; display: block; }
.dl-del  { color: var(--warn); background: #ff6b350c; display: block; }
.dl-hunk { color: var(--accent2); display: block; }
.dl-file { color: #94a3b8; font-weight: 700; display: block; }
.dl-meta { color: var(--muted); display: block; }

/* token popup */
.overlay { display: none; position: fixed; inset: 0; z-index: 300; align-items: center; justify-content: center; }
.overlay.on { display: flex; }
.overlay-bg { position: absolute; inset: 0; background: #000000aa; }
.overlay-box { position: relative; z-index: 1; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.5rem 1.75rem; max-width: 370px; width: 90%; }
.overlay-box h3 { font-family: var(--head); font-size: 1rem; font-weight: 700; color: var(--accent); margin-bottom: 1rem; }
.overlay-intro { font-size: .76rem; line-height: 1.6; color: var(--muted); margin-bottom: 1rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
.overlay-box ol { padding-left: 1.1rem; display: flex; flex-direction: column; gap: .5rem; }
.overlay-box li { font-size: .78rem; line-height: 1.6; color: var(--text); }
.overlay-box li code { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; color: var(--accent2); font-size: .75rem; }
.overlay-box li a { color: var(--accent2); }
.overlay-close { margin-top: 1.1rem; width: 100%; padding: .5rem; background: transparent; border: 1px solid var(--border); border-radius: 7px; color: var(--muted); font-family: var(--mono); font-size: .75rem; cursor: pointer; transition: all .15s; }
.overlay-close:hover { border-color: var(--accent); color: var(--accent); }

/* Settings */
#settings-shell { position: absolute; inset: 0; display: none; overflow-y: auto; padding: 2rem; }
#settings-shell.on { display: block; }
.settings-center { max-width: 520px; margin: 0 auto; }
.settings-title { font-family: var(--head); font-size: 1.5rem; font-weight: 800; color: var(--accent); margin-bottom: 1.25rem; }
.settings-desc { font-size: .8rem; color: var(--muted); margin: .3rem 0 1rem; }
.radio-row { display: flex; align-items: flex-start; gap: .7rem; padding: .7rem; border: 1px solid var(--border); border-radius: 8px; margin-bottom: .6rem; cursor: pointer; transition: border-color .15s; font-size: .85rem; }
.radio-row:hover { border-color: var(--accent2); }
.radio-row input { margin-top: .2rem; accent-color: var(--accent); flex-shrink: 0; }
.radio-sub { color: var(--muted); font-size: .72rem; }

::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }
</style>
</head>
<body>

<!-- Token popup -->
<div class="overlay" id="token-popup">
  <div class="overlay-bg" onclick="document.getElementById('token-popup').classList.remove('on')"></div>
  <div class="overlay-box">
    <h3>How to get a GitHub Token</h3>
    <p class="overlay-intro">Tokens are required for 60+ API requests/hr or for private repos.</p>
    <ol>
      <li>Go to <a href="https://github.com/settings/tokens" target="_blank">github.com/settings/tokens</a></li>
      <li>Click <strong>Generate new token (classic)</strong></li>
      <li>For public repos no scopes needed — just generate it. For private repos check the <code>repo</code> scope.</li>
      <li>Copy the token — it starts with <code>ghp_</code></li>
      <li>Paste it into the GitHub Token field</li>
    </ol>
    <button class="overlay-close" onclick="document.getElementById('token-popup').classList.remove('on')">Close</button>
  </div>
</div>

<div id="shell">
  <div id="topbar">
    <div class="logo">☢ DOOMVAULT</div>
    <div class="tabs">
      <button class="tab-btn on"  id="tab-vault"    onclick="switchTab('vault')">⬡ Vault</button>
      <button class="tab-btn"     id="tab-explore"  onclick="switchTab('explore')">◈ Explore</button>
      <button class="tab-btn"     id="tab-settings" onclick="switchTab('settings')">⚙ Settings</button>
    </div>
  </div>

  <div id="body">

    <!-- VAULT VIEW -->
    <div class="view on" id="view-vault">
      <div class="vault-center">
        <div class="v-hero">
          <div class="skull">☢</div>
          <h1>DOOMVAULT</h1>
          <p>Full offline GitHub archive. Paste. Vault. Done.</p>
        </div>
        <div class="card">
          <div class="field">
            <label>GitHub Repo</label>
            <input type="text" id="f-repo" placeholder="owner/repo  or  https://github.com/owner/repo">
          </div>
          <div class="field">
            <label>Save To Folder</label>
            <div class="row2">
              <input type="text" id="f-dest" placeholder="Click Browse or type a path…">
              <button class="ghost-btn" onclick="browseInto('f-dest')">📂 Browse</button>
            </div>
          </div>
          <div class="field">
            <div class="lbl-row">
              <label>GitHub Token <span style="color:var(--muted);font-size:.62rem;text-transform:none">(optional)</span></label>
              <button class="info-dot" onclick="document.getElementById('token-popup').classList.add('on')">i</button>
            </div>
            <input type="text" id="f-token" placeholder="ghp_xxxxxxxxxxxxxxxxxxxx">
          </div>
          <div class="field">
            <label>Options</label>
            <div class="toggles">
              <div class="tog on" id="tog-snap" onclick="this.classList.toggle('on')">📸 Download Snapshots</div>
              <div class="tog on" id="tog-rel"  onclick="this.classList.toggle('on')">📦 Download Assets</div>
            </div>
          </div>
          <div class="field">
            <label>Commit History Limit</label>
            <div class="row2">
              <input type="text" id="f-commits" value="500">
              <button class="ghost-btn v2" onclick="document.getElementById('f-commits').value='10000'">10k</button>
            </div>
          </div>
          <div class="field">
            <label>Version Range</label>
            <div class="hint hint-block">(applies to both releases and snapshots)</div>
            <div class="two-col">
              <div>
                <div class="sub-label">Limit</div>
                <div class="hint hint-block">(newest first, 0 = all)</div>
                <div class="row2">
                  <input type="text" id="f-rellimit" value="0">
                  <button class="ghost-btn v2" onclick="document.getElementById('f-rellimit').value='0'">All</button>
                </div>
              </div>
              <div>
                <div class="sub-label">Since Year</div>
                <div class="hint hint-block">(0 = no limit)</div>
                <div class="row2">
                  <input type="text" id="f-relyear" value="0">
                  <button class="ghost-btn v2" onclick="document.getElementById('f-relyear').value='0'">Off</button>
                </div>
              </div>
            </div>
          </div>
          <div class="action-row">
            <button class="vault-btn" id="vault-btn" onclick="startVault()">☢ VAULT IT</button>
            <button class="estimate-btn" id="estimate-btn" onclick="estimateDownload()">Estimate Download</button>
            <button class="cancel-btn" id="cancel-btn" onclick="cancelVault()">✕ Cancel</button>
          </div>
          <div id="estimate-result"></div>
        </div>

        <div id="prog-wrap">
          <div id="prog-bar-shell">
            <div class="pb-top">
              <span class="pb-step" id="pb-step">Starting…</span>
              <span class="pb-eta"  id="pb-eta"></span>
              <span class="pb-pct"  id="pb-pct">0%</span>
            </div>
            <div class="pb-track"><div class="pb-fill" id="pb-fill"></div></div>
          </div>
          <div id="log-box"></div>
          <div class="vault-done-msg" id="vault-done-msg" onclick="goToExplore()">✅ Archive complete — view it in the Explore tab →</div>
        </div>
      </div>
    </div><!-- /view-vault -->

    <!-- EXPLORE VIEW -->
    <div id="explore-shell">
      <div id="vault-sidebar">
        <div class="sidebar-hdr">
          My Vaults
          <div style="display:flex;gap:.35rem;">
            <button class="add-btn" onclick="refreshVaults()" title="Re-scan folders for changes">⟳</button>
            <button class="add-btn" onclick="addVault()">+ Add</button>
          </div>
        </div>
        <div id="vault-list"></div>
      </div>
      <div id="vault-detail">
        <div id="detail-empty">◈<br><br>Click <strong>+ Add</strong> to load an archive folder</div>
      </div>
    </div><!-- /explore-shell -->

    <!-- SETTINGS VIEW -->
    <div id="settings-shell">
      <div class="settings-center">
        <h2 class="settings-title">⚙ Settings</h2>
        <div class="card">
          <div class="lbl">Asset click behavior</div>
          <p class="settings-desc">When you click a downloaded asset in the Explore tab:</p>
          <label class="radio-row">
            <input type="radio" name="assetmode" value="reveal" checked onchange="setAssetMode('reveal')">
            <span><strong>Show in file explorer</strong><br><span class="radio-sub">Opens the folder and highlights the file</span></span>
          </label>
          <label class="radio-row">
            <input type="radio" name="assetmode" value="copy" onchange="setAssetMode('copy')">
            <span><strong>Copy to Downloads</strong><br><span class="radio-sub">Copies the file into your Downloads folder</span></span>
          </label>
        </div>
      </div>
    </div><!-- /settings-shell -->

  </div><!-- /body -->
</div><!-- /shell -->

<script>
// ═══════════════════════════════════════════════════════════════
// TAB SWITCHING
// ═══════════════════════════════════════════════════════════════
function switchTab(name) {
  var views = { vault: 'view-vault', explore: 'explore-shell', settings: 'settings-shell' };
  var disp  = { vault: 'block', explore: 'flex', settings: 'block' };
  for (var key in views) {
    var el = document.getElementById(views[key]);
    var active = (key === name);
    el.style.display = active ? disp[key] : 'none';
    el.classList.toggle('on', active);
    document.getElementById('tab-' + key).classList.toggle('on', active);
  }
  if (name === 'explore' && typeof loadVaultList === 'function' && !vaults.length) {
    loadVaultList();
  }
}

// ═══════════════════════════════════════════════════════════════
// VAULT TAB
// ═══════════════════════════════════════════════════════════════
let doneHtml = null;
let etaTimer = null;
let vaultStart = 0;
let curPct = 0;

function browseInto(id) {
  fetch('/browse').then(r => r.json()).then(d => {
    if (d.path) document.getElementById(id).value = d.path;
  }).catch(() => {});
}

let lastProgress = null;   // latest progress_state from server

function setProg(pct, step) {
  curPct = pct;
  document.getElementById('pb-fill').style.width = pct + '%';
  document.getElementById('pb-pct').textContent  = pct + '%';
  document.getElementById('pb-step').textContent = step || '';
}

function fmtDur(sec) {
  if (!isFinite(sec) || sec <= 0) return '';
  if (sec < 60)   return '~' + Math.round(sec) + 's left';
  if (sec < 3600) return '~' + Math.round(sec / 60) + 'm left';
  return '~' + Math.floor(sec / 3600) + 'h ' + Math.round((sec % 3600) / 60) + 'm left';
}

function startEta() {
  vaultStart = Date.now();
  if (etaTimer) clearInterval(etaTimer);
  etaTimer = setInterval(() => {
    const el = document.getElementById('pb-eta');
    if (curPct <= 0 || curPct >= 100) { el.textContent = ''; return; }

    const p = lastProgress;
    // Prefer a byte-based estimate while assets are downloading
    if (p && p.bytes_total > 0 && p.bytes_done > 0 && p.dl_start > 0) {
      const dlElapsed = (Date.now() / 1000) - p.dl_start;
      const rate      = p.bytes_done / dlElapsed;          // bytes/sec
      if (rate > 0) {
        const remBytes = Math.max(0, p.bytes_total - p.bytes_done);
        const doneMB   = (p.bytes_done / 1048576).toFixed(0);
        const totMB    = (p.bytes_total / 1048576).toFixed(0);
        el.textContent = fmtDur(remBytes / rate) + '  (' + doneMB + '/' + totMB + ' MB)';
        return;
      }
    }
    // Fallback: time-vs-percent extrapolation
    const elapsed = (Date.now() - vaultStart) / 1000;
    el.textContent = fmtDur(elapsed / curPct * (100 - curPct));
  }, 1000);
}

function stopEta() {
  if (etaTimer) { clearInterval(etaTimer); etaTimer = null; }
  document.getElementById('pb-eta').textContent = '';
}

function addLog(kind, msg) {
  const box = document.getElementById('log-box');
  const d   = document.createElement('div');
  d.className = 'l-' + kind;
  const icons = { STEP:'▶ ', OK:'✓ ', DONE:'✅ ', ERROR:'✗ ', WARN:'⚠ ', INFO:'  ', SKIP:'  ' };
  d.textContent = (icons[kind] || '  ') + msg;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

function estimateDownload() {
  const repo     = document.getElementById('f-repo').value.trim();
  const token    = document.getElementById('f-token').value.trim();
  const rellimit = parseInt(document.getElementById('f-rellimit').value) || 0;
  const relyear  = parseInt(document.getElementById('f-relyear').value) || 0;
  const wantAssets = document.getElementById('tog-rel').classList.contains('on');
  const wantSnaps  = document.getElementById('tog-snap').classList.contains('on');

  if (!repo) { alert('Enter a GitHub repo first.'); return; }

  const btn = document.getElementById('estimate-btn');
  const out = document.getElementById('estimate-result');
  btn.disabled = true;
  btn.textContent = 'Estimating…';
  out.innerHTML = '';

  const params = new URLSearchParams({
    repo: repo, token: token,
    rellimit: String(rellimit), relyear: String(relyear),
    assets: wantAssets ? '1' : '0',
    snapshots: wantSnaps ? '1' : '0'
  });

  fetch('/estimate?' + params.toString())
    .then(r => r.json())
    .then(d => {
      btn.disabled = false;
      btn.textContent = 'Estimate Download';
      if (d.error) { out.innerHTML = '<div class="est-box" style="border-color:var(--warn)">⚠ ' + esc(d.error) + '</div>'; return; }
      const fmtMB = (b) => {
        if (b >= 1073741824) return (b/1073741824).toFixed(2) + ' GB';
        if (b >= 1048576) return (b/1048576).toFixed(1) + ' MB';
        if (b >= 1024) return (b/1024).toFixed(1) + ' KB';
        return b + ' B';
      };
      const fmtTime = (s) => {
        if (s < 60) return '~' + s + 's';
        if (s < 3600) return '~' + Math.round(s/60) + ' min';
        return '~' + Math.floor(s/3600) + 'h ' + Math.round((s%3600)/60) + 'm';
      };
      const GB = d.total_bytes / 1073741824;
      const totalColor = GB >= 100 ? 'color:#ff4444;' : '';
      out.innerHTML = `
        <div class="est-box"${GB >= 100 ? ' style="border-color:#ff4444"' : ''}>
          <div class="est-total" style="${totalColor}">≈ ${fmtMB(d.total_bytes)}</div>
          <div class="est-line"><span>Estimated time</span><span>${fmtTime(d.est_seconds)}</span></div>
          <div class="est-line"><span>Releases included</span><span>${d.release_count}</span></div>
          <div class="est-line"><span>Snapshots</span><span>${d.snapshot_count > 0 ? d.snapshot_count + ' copies · ' + fmtMB(d.snapshot_bytes || 0) : 'skipped'}</span></div>
          <div class="est-line"><span>Release assets</span><span>${d.asset_included ? fmtMB(d.asset_bytes) : 'skipped'}</span></div>
          <div class="est-line"><span>Source archives (est.)</span><span>${fmtMB(d.source_bytes)}</span></div>
          <div class="est-line"><span>Git repo (est.)</span><span>${fmtMB(d.repo_bytes)}</span></div>
          <div class="est-note">${esc(d.note || '')}</div>
        </div>`;
      // Above 1000 GB (1 TB): show a prominent warning popup
      if (GB >= 500) {
        showSizeWarning(fmtMB(d.total_bytes));
      }
    })
    .catch(e => {
      btn.disabled = false;
      btn.textContent = 'Estimate Download';
      out.innerHTML = '<div class="est-box" style="border-color:var(--warn)">⚠ ' + esc(String(e)) + '</div>';
    });
}

function showSizeWarning(sizeText) {
  const overlay = document.createElement('div');
  overlay.className = 'overlay on';
  overlay.id = 'size-warning';
  overlay.innerHTML = `
    <div class="overlay-bg" onclick="document.getElementById('size-warning').remove()"></div>
    <div class="overlay-box" style="border-color:#ffd23f;text-align:center;">
      <div style="font-size:3rem;line-height:1;margin-bottom:.5rem;">⚠️</div>
      <h3 style="color:#ffd23f;">Very Large Download</h3>
      <p style="font-size:.85rem;line-height:1.6;color:var(--text);margin-bottom:1rem;">
        This archive is estimated at <strong>${esc(sizeText)}</strong>.<br><br>
        That's a very large download. Make sure your destination drive has enough free space before continuing.
        Consider turning off <strong>Download Snapshots</strong> or narrowing the Version Range to reduce the size.
      </p>
      <button class="overlay-close" style="border-color:#ffd23f;color:#ffd23f;" onclick="document.getElementById('size-warning').remove()">Got it</button>
    </div>`;
  document.body.appendChild(overlay);
}

function startVault() {
  const repo    = document.getElementById('f-repo').value.trim();
  const dest    = document.getElementById('f-dest').value.trim();
  const token   = document.getElementById('f-token').value.trim();
  const commits = parseInt(document.getElementById('f-commits').value) || 500;
  const rellimit = parseInt(document.getElementById('f-rellimit').value) || 0;
  const relyear = parseInt(document.getElementById('f-relyear').value) || 0;
  const snaps   = document.getElementById('tog-snap').classList.contains('on');
  const rels    = document.getElementById('tog-rel').classList.contains('on');

  if (!repo || !dest) { alert('Repo and folder are required.'); return; }

  document.getElementById('vault-btn').disabled    = true;
  document.getElementById('vault-btn').textContent = '⏳ Vaulting…';
  document.getElementById('cancel-btn').style.display = 'block';
  document.getElementById('prog-wrap').style.display  = 'block';
  document.getElementById('vault-done-msg').style.display = 'none';
  document.getElementById('log-box').innerHTML = '';
  doneHtml = null;
  setProg(0, 'Starting…');
  startEta();

  fetch('/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo, dest, token, commits, rellimit, relyear, snapshots: snaps, releases: rels })
  }).then(r => r.json()).then(d => {
    if (d.ok) pollLog();
    else { addLog('ERROR', d.error); resetVaultBtn(); }
  });
}

function cancelVault() {
  fetch('/cancel').then(() => { stopEta(); resetVaultBtn(); });
}

function resetVaultBtn() {
  document.getElementById('vault-btn').disabled    = false;
  document.getElementById('vault-btn').textContent = '☢ VAULT IT';
  document.getElementById('cancel-btn').style.display = 'none';
}

function pollLog() {
  fetch('/log').then(r => r.json()).then(data => {
    // Render new log lines
    const box   = document.getElementById('log-box');
    const count = box.children.length;
    (data.lines || []).slice(count).forEach(([k, m]) => addLog(k, m));

    if (data.progress) { lastProgress = data.progress; setProg(data.progress.pct, data.progress.step); }

    const lines = data.lines || [];
    const last  = lines[lines.length - 1];

    if (last && last[0] === 'DONE') {
      doneHtml = last[1];
      stopEta(); setProg(100, 'Complete 🎉');
      document.getElementById('vault-done-msg').style.display = 'block';
      resetVaultBtn();
      registerCompletedVault(last[1]);   // auto-add to Explore list
    } else if (last && (last[0] === 'ERROR' || (last[0] === 'WARN' && last[1] === 'Cancelled by user.'))) {
      stopEta(); resetVaultBtn();
    } else {
      setTimeout(pollLog, 800);
    }
  }).catch(() => setTimeout(pollLog, 1500));
}

function registerCompletedVault(htmlPath) {
  // htmlPath is .../<repo>/index.html — the vault folder is its parent.
  // Ask the server to resolve the folder + manifest, then add to the list.
  fetch('/registervault?html=' + encodeURIComponent(htmlPath))
    .then(r => r.json())
    .then(d => {
      if (d.error || !d.path) return;
      if (!vaults.find(v => v.path === d.path)) {
        vaults.push({ path: d.path, manifest: d.manifest || {}, data: null });
        saveVaultList();
        renderSidebar();
      }
    }).catch(() => {});
}

function goToExplore() {
  switchTab('explore');
  // select the most recently added vault if present
  if (vaults.length) selectVault(vaults.length - 1);
}

// ═══════════════════════════════════════════════════════════════
// EXPLORE TAB
// ═══════════════════════════════════════════════════════════════
let vaults       = [];
let activeVault  = -1;
let activeTab    = 'releases';
let relPage      = 0;
let comPage      = 0;
let filteredComs = [];
const REL_PER   = 5;
const COM_PER   = 25;

function loadVaultList() {
  fetch('/vaults').then(r => r.json()).then(d => {
    vaults = d.vaults || [];
    renderSidebar();
  }).catch(() => {});
}

function refreshVaults() {
  fetch('/refresh').then(r => r.json()).then(d => {
    const removed = d.removed || 0;
    vaults = (d.vaults || []).map(v => ({ path: v.path, manifest: v.manifest, data: null }));
    // keep selection if the active vault still exists
    if (activeVault >= vaults.length) activeVault = -1;
    renderSidebar();
    if (activeVault >= 0) selectVault(activeVault);
    else document.getElementById('vault-detail').innerHTML =
      '<div id="detail-empty">◈<br><br>Click <strong>+ Add</strong> to load an archive folder</div>';
    if (removed > 0) alert(removed + ' vault' + (removed > 1 ? 's' : '') + ' removed (folder no longer exists).');
  }).catch(() => {});
}

function saveVaultList() {
  fetch('/vaults', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vaults: vaults.map(v => ({ path: v.path, manifest: v.manifest })) })
  }).catch(() => {});
}

function addVault() {
  fetch('/browse').then(r => r.json()).then(d => {
    if (!d.path) return;
    if (vaults.find(v => v.path === d.path)) { alert('This folder is already in your list.'); return; }
    fetch('/manifest?path=' + encodeURIComponent(d.path))
      .then(r => r.json())
      .then(m => {
        if (m.error) { alert('Could not load vault:\n\n' + m.error); return; }
        vaults.push({ path: d.path, manifest: m, data: null });
        saveVaultList();
        renderSidebar();
        selectVault(vaults.length - 1);
      });
  }).catch(() => {});
}

function renderSidebar() {
  const el = document.getElementById('vault-list');
  if (!vaults.length) {
    el.innerHTML = '<div class="v-empty">No vaults yet.<br>Click + Add to browse<br>to an archive folder.</div>';
    return;
  }
  el.innerHTML = vaults.map((v, i) => {
    const m = v.manifest || {};
    const name = m.repo || v.path.split(/[\\\\/]/).pop();
    const meta = [m.owner ? m.owner + '/' + name : '', m.releases ? m.releases + ' rel' : '', m.commits ? m.commits + ' commits' : ''].filter(Boolean).join(' · ');
    return `<div class="v-item${activeVault === i ? ' on' : ''}" onclick="selectVault(${i})">
      <div class="vi-name">${esc(name)}</div>
      <div class="vi-meta">${esc(meta)}</div>
    </div>`;
  }).join('');
}

function selectVault(i) {
  activeVault = i;
  relPage = 0; comPage = 0; filteredComs = [];
  renderSidebar();
  const v = vaults[i];
  if (v.data) { renderDetail(); return; }
  document.getElementById('vault-detail').innerHTML = '<div id="detail-empty">Loading…</div>';
  fetch('/vaultdata?path=' + encodeURIComponent(v.path))
    .then(r => r.json())
    .then(data => {
      v.data       = data;
      filteredComs = data.commits || [];
      renderDetail();
    })
    .catch(e => {
      document.getElementById('vault-detail').innerHTML = `<div id="detail-empty">Error: ${esc(String(e))}</div>`;
    });
}

// Tab definitions — order is user-customizable and persisted
const TAB_DEFS = {
  releases: { label: '🏷 Releases', render: renderRels },
  commits:  { label: '◎ Commits',  render: renderComs },
  readme:   { label: '📖 README',   render: renderReadme },
};
let tabOrder = ['releases', 'commits', 'readme'];

function loadTabOrder() {
  fetch('/gettaborder').then(r => r.json()).then(d => {
    if (Array.isArray(d.order) && d.order.length) {
      // keep only known tabs, append any missing
      const known = d.order.filter(t => TAB_DEFS[t]);
      ['releases','commits','readme'].forEach(t => { if (!known.includes(t)) known.push(t); });
      tabOrder = known;
    }
  }).catch(() => {});
}

function saveTabOrder() {
  fetch('/settaborder', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ order: tabOrder })
  }).catch(() => {});
}

function renderDetail() {
  const tabsHtml = tabOrder.map(t =>
    `<button class="dtab${activeTab===t?' on':''}" id="dt-${t}" draggable="true" onclick="setDTab('${t}')">${TAB_DEFS[t].label}</button>`
  ).join('');
  document.getElementById('vault-detail').innerHTML = `
    <div id="detail-tabs">
      <div class="dtab-group" id="dtab-group">${tabsHtml}</div>
      <button class="dl-updates-btn" id="dl-updates-btn" onclick="downloadUpdates()">
        ${ARROW_SVG}<span id="dl-updates-label">Download Updates</span>
      </button>
    </div>
    <div id="detail-body"></div>`;
  wireTabDragging();
  renderDetailBody();
  checkForUpdates();   // ping GitHub for new releases
}

// Thick downward arrow
const ARROW_SVG = '<svg class="dl-arrow" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="4" x2="12" y2="18"></line><polyline points="6 12 12 19 18 12"></polyline></svg>';
const CHECK_SVG = '<svg class="dl-arrow" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 12 10 18 20 6"></polyline></svg>';

let updatesAvailable = 0;   // how many new releases were found
let updatesChecking  = false;

function checkForUpdates() {
  const btn   = document.getElementById('dl-updates-btn');
  const label = document.getElementById('dl-updates-label');
  if (!btn || !label) return;
  const v = vaults[activeVault];
  if (!v) return;

  updatesChecking = true;
  btn.classList.remove('uptodate');
  label.textContent = 'Checking…';

  fetch('/checkupdates?path=' + encodeURIComponent(v.path))
    .then(r => r.json())
    .then(d => {
      updatesChecking = false;
      if (d.error) { label.textContent = 'Download Updates'; return; }
      updatesAvailable = d.new_count || 0;
      if (updatesAvailable > 0) {
        btn.classList.remove('uptodate');
        btn.innerHTML = ARROW_SVG + '<span id="dl-updates-label">Download ' + updatesAvailable + ' Updates</span>';
      } else {
        btn.classList.add('uptodate');
        btn.innerHTML = CHECK_SVG + '<span id="dl-updates-label">Up to Date</span>';
      }
    })
    .catch(() => { updatesChecking = false; label.textContent = 'Download Updates'; });
}

function downloadUpdates() {
  if (updatesChecking) return;
  const v = vaults[activeVault];
  if (!v) return;
  // Nothing to do if already up to date
  const btn = document.getElementById('dl-updates-btn');
  if (btn && btn.classList.contains('uptodate')) return;
  if (updatesAvailable <= 0) return;

  // Re-run the vault into the SAME parent folder; the worker fetches new
  // releases/commits and skips anything already downloaded.
  const parent = v.path.replace(/[\\\\/][^\\\\/]+$/, '');  // strip the repo-name folder
  const repo   = (v.manifest && v.manifest.owner ? v.manifest.owner + '/' : '') + (v.manifest && v.manifest.repo ? v.manifest.repo : v.path.split(/[\\\\/]/).pop());

  if (btn) { btn.disabled = true; btn.innerHTML = ARROW_SVG + '<span>Downloading…</span>'; }

  fetch('/start', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo: repo, dest: parent, token: '', commits: 10000, rellimit: 0, relyear: 0, snapshots: true, releases: true })
  }).then(r => r.json()).then(d => {
    if (!d.ok) { alert('Update failed: ' + (d.error || 'unknown')); if (btn) btn.disabled = false; return; }
    pollUpdate(btn);
  }).catch(e => { alert('Update error: ' + e); if (btn) btn.disabled = false; });
}

function pollUpdate(btn) {
  fetch('/log').then(r => r.json()).then(data => {
    const lines = data.lines || [];
    const last  = lines[lines.length - 1];
    if (last && last[0] === 'DONE') {
      // refresh this vault's data and re-check
      const v = vaults[activeVault];
      v.data = null;
      if (btn) btn.disabled = false;
      selectVault(activeVault);   // reloads data + re-renders + re-checks updates
    } else if (last && last[0] === 'ERROR') {
      alert('Update failed: ' + last[1]);
      if (btn) btn.disabled = false;
    } else {
      setTimeout(() => pollUpdate(btn), 900);
    }
  }).catch(() => setTimeout(() => pollUpdate(btn), 1500));
}

function setDTab(tab) {
  activeTab = tab;
  relPage = 0; comPage = 0;
  document.querySelectorAll('.dtab').forEach(b => b.classList.remove('on'));
  var el = document.getElementById('dt-' + tab);
  if (el) el.classList.add('on');
  renderDetailBody();
}

function renderDetailBody() {
  if (!TAB_DEFS[activeTab]) activeTab = tabOrder[0];
  TAB_DEFS[activeTab].render();
}

// ── Drag-to-reorder tabs ──────────────────────────────────────────────────
let dragTab = null;
function wireTabDragging() {
  const tabs = document.querySelectorAll('#dtab-group .dtab');
  tabs.forEach(tab => {
    tab.addEventListener('dragstart', e => { dragTab = tab.id.replace('dt-',''); tab.classList.add('dragging'); });
    tab.addEventListener('dragend',   e => { tab.classList.remove('dragging'); });
    tab.addEventListener('dragover',  e => { e.preventDefault(); });
    tab.addEventListener('drop', e => {
      e.preventDefault();
      const target = tab.id.replace('dt-','');
      if (!dragTab || dragTab === target) return;
      const from = tabOrder.indexOf(dragTab);
      const to   = tabOrder.indexOf(target);
      tabOrder.splice(from, 1);
      tabOrder.splice(to, 0, dragTab);
      saveTabOrder();
      renderDetail();
    });
  });
}

function renderReadme() {
  const body = document.getElementById('detail-body');
  if (!body) return;
  const v = vaults[activeVault];
  body.innerHTML = '<div style="padding:2rem;color:var(--muted)">Loading README…</div>';
  fetch('/readme?path=' + encodeURIComponent(v.path))
    .then(r => r.json())
    .then(d => {
      if (d.error || !d.content) {
        body.innerHTML = '<div class="empty" style="padding:3rem"><span>📖</span>No README found in this archive.</div>';
        return;
      }
      body.innerHTML = '<div class="readme-wrap md">' + mdToHtml(d.content, v.path) + '</div>';
    })
    .catch(e => { body.innerHTML = '<div style="padding:2rem;color:var(--warn)">Error: ' + esc(String(e)) + '</div>'; });
}

function renderRels() {
  const body = document.getElementById('detail-body');
  if (!body) return;
  const v    = vaults[activeVault];
  const all  = (v && v.data && v.data.releases) || [];
  const pages = Math.max(1, Math.ceil(all.length / REL_PER));
  relPage     = Math.min(relPage, pages - 1);
  const slice = all.slice(relPage * REL_PER, (relPage + 1) * REL_PER);

  if (!all.length) { body.innerHTML = '<div style="text-align:center;color:var(--muted);padding:3rem">No releases in this archive.</div>'; return; }

  body.innerHTML = `
    <div class="d-releases">${slice.map((r, si) => {
      const gi  = relPage * REL_PER + si;
      const b   = r.prerelease ? '<span class="d-badge d-pre">pre</span>' : '<span class="d-badge d-stable">stable</span>';
      const dt  = r.published_at ? new Date(r.published_at).toLocaleDateString('en-US', {year:'numeric',month:'short',day:'numeric'}) : '';
      const vpath = vaults[activeVault] ? vaults[activeVault].path : '';
      const ass = (r.assets || []).map(a => `<div class="d-asset-row" onclick="assetClick('${escAttr(vpath)}','${escAttr(a.local_path)}')" title="${esc(a.name)}"><span class="d-asset-icon">📦</span><span class="d-asset-name">${esc(a.name)}</span><span class="d-asset-sz">${fmtb(a.size)}</span></div>`).join('');
      return `<div class="d-rel-card" id="drc${gi}">
        <div class="d-rel-head" onclick="document.getElementById('drc${gi}').classList.toggle('open')">
          <div class="d-rel-head-left">
            <div class="d-tag-line"><span class="d-tag">${esc(r.tag_name)}</span>${b}</div>
            ${r.name && r.name !== r.tag_name ? `<div style="font-size:.75rem;color:var(--muted);margin-top:.15rem">${esc(r.name)}</div>` : ''}
            <div class="d-rel-meta">
              ${r.author ? `<span>👤 ${esc(r.author)}</span>` : ''}
              ${dt ? `<span>📅 ${dt}</span>` : ''}
              <span>📎 ${(r.assets || []).length} assets</span>
            </div>
          </div>
          <span class="d-chev">⌄</span>
        </div>
        <div class="d-rel-body">
          ${r.body && r.body.trim() ? `<div class="d-notes md">${mdToHtml(r.body)}</div>` : '<div class="d-notes-empty">No release notes.</div>'}
          ${ass ? `<div class="d-assets"><h4>Assets</h4><div class="d-asset-list">${ass}</div></div>` : ''}
        </div>
      </div>`;
    }).join('')}</div>
    <div class="pager">
      <button class="pg-btn" onclick="relPage--;renderRels()" ${relPage===0?'disabled':''}>← Prev</button>
      <span>Page ${relPage+1} of ${pages} · ${all.length} releases</span>
      <button class="pg-btn" onclick="relPage++;renderRels()" ${relPage>=pages-1?'disabled':''}>Next →</button>
    </div>`;
}

function renderComs() {
  const body  = document.getElementById('detail-body');
  if (!body) return;
  const pages = Math.max(1, Math.ceil(filteredComs.length / COM_PER));
  comPage     = Math.min(comPage, pages - 1);
  const slice = filteredComs.slice(comPage * COM_PER, (comPage + 1) * COM_PER);

  const vpath = vaults[activeVault] ? vaults[activeVault].path : '';
  body.innerHTML = `
    <div class="c-search"><input type="text" placeholder="Filter by message, author, hash…" oninput="filterComs(this.value)"></div>
    <table class="c-table">
      <thead><tr><th></th><th>Hash</th><th>Subject</th><th>Author</th><th>Date</th></tr></thead>
      <tbody>${slice.length ? slice.map(c => `<tr class="c-row" onclick="showCommit('${escAttr(c.hash)}')">
        <td><button class="c-export-btn" title="Export archive of this version" onclick="event.stopPropagation();exportSnapshot('${escAttr(vpath)}','${escAttr(c.hash)}','${escAttr(c.short)}')">Export Archive</button></td>
        <td><span class="c-hash">${esc(c.short)}</span></td>
        <td>${esc(c.subject)}</td>
        <td class="c-author">${esc(c.author)}</td>
        <td class="c-date">${c.date ? c.date.slice(0,10) : ''}</td>
      </tr>`).join('') : '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:2rem">No commits match.</td></tr>'}</tbody>
    </table>
    <div class="pager">
      <button class="pg-btn" onclick="comPage--;renderComs()" ${comPage===0?'disabled':''}>← Prev</button>
      <span>Page ${comPage+1} of ${pages} · ${filteredComs.length} commits</span>
      <button class="pg-btn" onclick="comPage++;renderComs()" ${comPage>=pages-1?'disabled':''}>Next →</button>
    </div>`;
}

function filterComs(q) {
  const v = vaults[activeVault];
  const all = (v && v.data && v.data.commits) || [];
  filteredComs = q ? all.filter(c =>
    c.subject.toLowerCase().includes(q.toLowerCase()) ||
    c.author.toLowerCase().includes(q.toLowerCase()) ||
    c.short.toLowerCase().includes(q.toLowerCase())
  ) : all;
  comPage = 0;
  renderComs();
}

function showCommit(hash) {
  const v = vaults[activeVault];
  if (!v) return;
  const body = document.getElementById('detail-body');
  body.innerHTML = '<div style="padding:2rem;color:var(--muted)">Loading commit…</div>';
  fetch('/commit?path=' + encodeURIComponent(v.path) + '&hash=' + encodeURIComponent(hash))
    .then(r => r.json())
    .then(d => {
      if (d.error) { body.innerHTML = '<div style="padding:2rem;color:var(--warn)">Error: ' + esc(d.error) + '</div>'; return; }
      renderCommitDetail(d);
    })
    .catch(e => { body.innerHTML = '<div style="padding:2rem;color:var(--warn)">Error: ' + esc(String(e)) + '</div>'; });
}

function renderCommitDetail(d) {
  const body = document.getElementById('detail-body');

  // File stat list
  const files = (d.files || []).map(f => {
    const cls = f.status === 'A' ? 'fstat-add' : f.status === 'D' ? 'fstat-del' : 'fstat-mod';
    const tag = f.status === 'A' ? 'added' : f.status === 'D' ? 'deleted' : 'modified';
    return `<div class="fstat-row"><span class="fstat-badge ${cls}">${tag}</span><span class="fstat-name">${esc(f.name)}</span></div>`;
  }).join('');

  // Diff with colored lines
  const diffHtml = colorizeDiff(d.diff || '');

  const vpath = vaults[activeVault] ? vaults[activeVault].path : '';
  body.innerHTML = `
    <div class="cd-topbar">
      <button class="back-btn" onclick="renderComs()">← Back to commits</button>
      <button class="export-btn" onclick="exportSnapshot('${escAttr(vpath)}','${escAttr(d.hash || '')}','${escAttr(d.short || '')}')">Export Archive</button>
    </div>
    <div class="commit-detail">
      <div class="cd-subject">${esc(d.subject || '')}</div>
      ${d.body ? `<div class="cd-body">${esc(d.body)}</div>` : ''}
      <div class="cd-meta">
        <span class="c-hash">${esc(d.short || '')}</span>
        <span>👤 ${esc(d.author || '')}</span>
        <span>📅 ${d.date ? d.date.slice(0,19).replace('T',' ') : ''}</span>
        <span>📄 ${(d.files || []).length} file${(d.files||[]).length!==1?'s':''} changed</span>
      </div>
      ${files ? `<div class="cd-files">${files}</div>` : ''}
      ${diffHtml ? `<div class="cd-diff"><pre>${diffHtml}</pre></div>` : '<div style="color:var(--muted);font-size:.78rem;padding:1rem 0">No textual diff (binary or empty).</div>'}
    </div>`;
}

function exportSnapshot(vaultPath, hash, shortHash) {
  if (!vaultPath || !hash) return;
  // Ask user to pick a destination folder, then export there
  fetch('/browse').then(r => r.json()).then(d => {
    if (!d.path) return;  // cancelled
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:#000000cc;z-index:500;display:flex;align-items:center;justify-content:center;';
    overlay.innerHTML = '<div style="background:#111118;border:1px solid #1e1e2e;border-radius:12px;padding:1.5rem 2rem;color:#e2e8f0;font-size:.85rem;">⏳ Exporting version ' + esc(shortHash) + '…</div>';
    document.body.appendChild(overlay);
    fetch('/exportcommit?path=' + encodeURIComponent(vaultPath) + '&hash=' + encodeURIComponent(hash) + '&dest=' + encodeURIComponent(d.path))
      .then(r => r.json())
      .then(res => {
        overlay.remove();
        if (res.error) { alert('Export failed:\n\n' + res.error); return; }
        const note = document.createElement('div');
        note.innerHTML = '✓ Exported ' + (res.count || '') + ' files to:<br><strong>' + esc(res.dest || '') + '</strong>';
        note.style.cssText = 'position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:#00ff9d;color:#0a0a0f;padding:.8rem 1.4rem;border-radius:8px;font-size:.8rem;font-weight:600;z-index:999;text-align:center;line-height:1.5;max-width:80%;';
        document.body.appendChild(note);
        setTimeout(() => note.remove(), 4000);
      })
      .catch(e => { overlay.remove(); alert('Export error: ' + e); });
  }).catch(() => {});
}

function colorizeDiff(diff) {
  if (!diff) return '';
  return diff.split(String.fromCharCode(10)).map(line => {
    const e = esc(line);
    if (line.startsWith('+++') || line.startsWith('---')) return '<span class="dl-file">' + e + '</span>';
    if (line.startsWith('@@'))  return '<span class="dl-hunk">' + e + '</span>';
    if (line.startsWith('+'))   return '<span class="dl-add">' + e + '</span>';
    if (line.startsWith('-'))   return '<span class="dl-del">' + e + '</span>';
    if (line.startsWith('diff ') || line.startsWith('index ')) return '<span class="dl-meta">' + e + '</span>';
    return e;
  }).join(String.fromCharCode(10));
}

// ═══════════════════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════════════════
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtb(b) {
  if (!b) return '?';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}
// Escape a string for safe use inside an HTML attribute / onclick arg
function escAttr(s) {
  if (!s) return '';
  return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// ── Asset click behavior (reveal in explorer OR copy to Downloads) ────────────
let assetMode = 'reveal';   // 'reveal' or 'copy'

function setAssetMode(mode) {
  assetMode = mode;
  fetch('/setassetmode?mode=' + encodeURIComponent(mode)).catch(() => {});
}

function loadAssetMode() {
  fetch('/getassetmode').then(r => r.json()).then(d => {
    assetMode = d.mode || 'reveal';
    const radio = document.querySelector('input[name="assetmode"][value="' + assetMode + '"]');
    if (radio) radio.checked = true;
  }).catch(() => {});
}

function assetClick(vaultPath, relPath) {
  const ep = (assetMode === 'copy') ? '/copyasset' : '/revealasset';
  fetch(ep + '?vault=' + encodeURIComponent(vaultPath) + '&rel=' + encodeURIComponent(relPath))
    .then(r => r.json())
    .then(d => {
      if (d.error) { alert('Could not ' + (assetMode === 'copy' ? 'copy' : 'reveal') + ' file:\\n\\n' + d.error); return; }
      if (assetMode === 'copy' && d.dest) {
        // brief confirmation
        const note = document.createElement('div');
        note.textContent = '✓ Copied to ' + d.dest;
        note.style.cssText = 'position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);background:#00ff9d;color:#0a0a0f;padding:.6rem 1.2rem;border-radius:8px;font-size:.8rem;font-weight:700;z-index:999;';
        document.body.appendChild(note);
        setTimeout(() => note.remove(), 2500);
      }
    })
    .catch(e => alert('Error: ' + e));
}

// ── Lightweight Markdown → HTML (GitHub-flavored subset) ──────────────────────
function mdToHtml(src, imgBase) {
  if (!src) return '';
  imgBase = imgBase || '';
  var NL = String.fromCharCode(10);
  var SENT = String.fromCharCode(1);
  var SENT2 = String.fromCharCode(2);
  src = src.split(String.fromCharCode(13) + NL).join(NL).split(String.fromCharCode(13)).join(NL);

  // Extract fenced code blocks
  var codeBlocks = [];
  src = src.replace(/```[^\n]*\n([\s\S]*?)```/g, function(m, code){
    codeBlocks.push(code.replace(/\n$/, ''));
    return SENT + 'CODE' + (codeBlocks.length - 1) + SENT;
  });

  // Extract raw HTML blocks/tags (GitHub allows HTML inside markdown).
  // We keep a safe subset verbatim and re-insert after processing.
  var htmlBlocks = [];
  // block-level HTML elements that commonly appear in READMEs
  var blockTags = 'p|div|img|a|br|hr|table|thead|tbody|tr|td|th|h1|h2|h3|h4|h5|h6|ul|ol|li|b|strong|i|em|code|pre|sub|sup|details|summary|picture|source|span|center|kbd|blockquote';
  var tagRe = new RegExp('<\\/?(?:' + blockTags + ')(?:\\s[^>]*)?\\/?>', 'gi');
  src = src.replace(tagRe, function(tag){
    htmlBlocks.push(sanitizeTag(tag, imgBase));
    return SENT2 + (htmlBlocks.length - 1) + SENT2;
  });

  function inline(t) {
    t = esc(t);
    // restore raw-html placeholders (they were escaped above)
    t = t.replace(new RegExp(SENT2 + '(\\d+)' + SENT2, 'g'), function(m, n){ return htmlBlocks[+n]; });
    t = t.replace(/`([^`]+)`/g, function(m, c){ return '<code>' + c + '</code>'; });
    t = t.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, function(m, alt, url){ return '<img alt="' + alt + '" src="' + resolveImg(url, imgBase) + '">'; });
    t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
    t = t.replace(/(^|[^_])_([^_]+)_/g, '$1<em>$2</em>');
    t = t.replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank">$2</a>');
    return t;
  }

  var lines = src.split(NL);
  var html = '', i = 0;
  var sentRe = new RegExp('^' + SENT + 'CODE(\\d+)' + SENT + '$');

  while (i < lines.length) {
    var line = lines[i];

    var cb = line.match(sentRe);
    if (cb) { html += '<pre><code>' + esc(codeBlocks[+cb[1]]) + '</code></pre>'; i++; continue; }

    var h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { var lvl = h[1].length; html += '<h' + lvl + '>' + inline(h[2]) + '</h' + lvl + '>'; i++; continue; }

    if (/^(\s*[-*_]){3,}\s*$/.test(line)) { html += '<hr>'; i++; continue; }

    if (/^>\s?/.test(line)) {
      var quote = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { quote.push(lines[i].replace(/^>\s?/, '')); i++; }
      html += '<blockquote>' + inline(quote.join(' ')) + '</blockquote>';
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      var items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push('<li>' + inline(lines[i].replace(/^\s*[-*+]\s+/, '')) + '</li>'); i++;
      }
      html += '<ul>' + items.join('') + '</ul>';
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      var items2 = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items2.push('<li>' + inline(lines[i].replace(/^\s*\d+\.\s+/, '')) + '</li>'); i++;
      }
      html += '<ol>' + items2.join('') + '</ol>';
      continue;
    }

    if (line.trim() === '') { i++; continue; }

    var para = [];
    while (i < lines.length && lines[i].trim() !== ''
           && !/^(#{1,6})\s/.test(lines[i])
           && !/^\s*[-*+]\s+/.test(lines[i])
           && !/^\s*\d+\.\s+/.test(lines[i])
           && !/^>\s?/.test(lines[i])
           && !sentRe.test(lines[i])
           && !/^(\s*[-*_]){3,}\s*$/.test(lines[i])) {
      para.push(lines[i]); i++;
    }
    if (para.length) html += '<p>' + inline(para.join(' ')) + '</p>';
  }
  return html;
}

// Resolve a relative image path to a servable URL through the vault
function resolveImg(url, imgBase) {
  if (!url) return '';
  url = url.trim().replace(/^["']|["']$/g, '');
  if (/^(https?:|data:)/i.test(url)) return url;        // absolute — leave as-is
  if (!imgBase) return url;
  var clean = url.replace(/^\.\//, '').replace(/^\//, '');
  return '/vaultfile?path=' + encodeURIComponent(imgBase) + '&rel=' + encodeURIComponent(clean);
}

// Lightly sanitize a raw HTML tag: drop on* handlers and rewrite relative img src
function sanitizeTag(tag, imgBase) {
  // strip event handler attributes like onclick=, onerror=
  tag = tag.replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '');
  // (script and style tags are not in the allowed blockTags list, so they never reach here)
  // rewrite img/source src + srcset relative paths
  tag = tag.replace(/(\b(?:src|srcset)\s*=\s*)("([^"]*)"|'([^']*)')/gi, function(m, pre, q, dq, sq){
    var val = (dq !== undefined ? dq : sq) || '';
    return pre + '"' + resolveImg(val, imgBase) + '"';
  });
  return tag;
}

// ═══════════════════════════════════════════════════════════════
// KEEPALIVE — ping every 4s; server shuts down shortly after pings stop
// ═══════════════════════════════════════════════════════════════
setInterval(() => fetch('/ping').catch(() => {}), 4000);

// Tell the server to quit the moment the tab is closing. We listen on
// several events because browsers don't fire 'beforeunload' reliably for
// beacons — 'pagehide' is the most dependable for actual tab/window close.
let saidGoodbye = false;
function sayGoodbye() {
  if (saidGoodbye) return;
  saidGoodbye = true;
  try { navigator.sendBeacon('/goodbye'); } catch (e) {}
}
window.addEventListener('pagehide', sayGoodbye);
window.addEventListener('beforeunload', sayGoodbye);
// If the tab is hidden for a while (closed in background), also signal.
// We DON'T quit on brief hides (switching tabs), only confirm via the watchdog.

// ═══════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════
loadVaultList();
loadAssetMode();
loadTabOrder();
</script>
</body>
</html>"""

# ─── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global last_ping, browser_connected
        from urllib.parse import urlparse, parse_qs, unquote
        path = urlparse(self.path).path
        qs   = parse_qs(urlparse(self.path).query)

        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        elif path == "/" or path == "/index.html":
            body = UI_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/ping":
            last_ping = time.time()
            browser_connected = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        elif path == "/goodbye":
            self.send_response(200)
            self.end_headers()
            threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()

        elif path == "/log":
            self.send_json({"lines": log_lines, "progress": progress_state})

        elif path == "/vaults":
            self.send_json({"vaults": load_vaults()})

        elif path == "/refresh":
            # Re-scan saved vaults: drop folders that no longer exist,
            # re-read manifest/counts for the ones that remain.
            saved   = load_vaults()
            kept    = []
            removed = 0
            for v in saved:
                folder = Path(v.get("path", ""))
                if not folder.exists():
                    removed += 1
                    continue
                # rebuild a fresh manifest the same way /manifest does
                m = {"repo": folder.name}
                try:
                    mf = folder / "manifest.json"
                    if mf.exists():
                        m = json.loads(mf.read_text(encoding="utf-8"))
                    else:
                        rel_idx = folder / "releases" / "index.json"
                        if rel_idx.exists():
                            rels = json.loads(rel_idx.read_text(encoding="utf-8"))
                            m["releases"] = len(rels)
                            if rels and rels[0].get("html_url"):
                                parts = rels[0]["html_url"].replace("https://github.com/","").split("/")
                                if len(parts) >= 2:
                                    m["owner"] = parts[0]; m["repo"] = parts[1]
                        mirror = folder / "_git_mirror"
                        if not mirror.exists() and (folder / "HEAD").exists():
                            mirror = folder
                        if mirror.exists():
                            r = subprocess.run(["git","rev-list","--count","HEAD"],
                                               cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace")
                            try: m["commits"] = int(r.stdout.strip())
                            except: m["commits"] = 0
                except Exception:
                    pass
                kept.append({"path": v["path"], "manifest": m})
            save_vaults(kept)
            self.send_json({"vaults": kept, "removed": removed})

        elif path == "/exportcommit":
            folder = Path(unquote(qs.get("path", [""])[0]))
            chash  = unquote(qs.get("hash", [""])[0])
            dest_base = Path(unquote(qs.get("dest", [""])[0]))
            try:
                mirror = folder / "_git_mirror"
                if not mirror.exists() and (folder / "HEAD").exists():
                    mirror = folder
                if not mirror.exists():
                    self.send_json({"error": "git mirror not found in vault"}); return
                if not dest_base.exists():
                    self.send_json({"error": f"destination folder does not exist:\n{dest_base}"}); return

                # repo name + short hash for the export folder name
                repo_name = folder.name
                short = chash[:8]
                out_dir = dest_base / f"{repo_name}@{short}"
                # avoid clobbering
                if out_dir.exists():
                    n = 1
                    while (dest_base / f"{repo_name}@{short} ({n})").exists():
                        n += 1
                    out_dir = dest_base / f"{repo_name}@{short} ({n})"
                out_dir.mkdir(parents=True, exist_ok=True)

                # export the exact tree at this commit via git archive
                tmp_tar = dest_base / f"_export_{short}.tar"
                r = subprocess.run(
                    ["git", "archive", "--format=tar", f"--output={tmp_tar}", chash],
                    cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace"
                )
                if r.returncode != 0:
                    out_dir.rmdir() if out_dir.exists() and not any(out_dir.iterdir()) else None
                    self.send_json({"error": "git archive failed: " + (r.stderr or "unknown")}); return

                count = 0
                with tarfile.open(tmp_tar) as t:
                    members = t.getmembers()
                    count = sum(1 for m in members if m.isfile())
                    t.extractall(out_dir)
                try: tmp_tar.unlink()
                except Exception: pass

                self.send_json({"ok": True, "dest": str(out_dir), "count": count})
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/commit":
            folder = Path(unquote(qs.get("path", [""])[0]))
            chash  = unquote(qs.get("hash", [""])[0])
            try:
                mirror = folder / "_git_mirror"
                if not mirror.exists() and (folder / "HEAD").exists():
                    mirror = folder
                if not mirror.exists():
                    self.send_json({"error": "git mirror not found in vault"}); return
                # Basic metadata
                fmt = "%H%x00%h%x00%an%x00%ae%x00%aI%x00%s%x00%b"
                meta = subprocess.run(
                    ["git", "show", "-s", f"--pretty=format:{fmt}", chash],
                    cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace"
                ).stdout
                parts = meta.split("\x00")
                info = {
                    "hash": parts[0] if len(parts) > 0 else chash,
                    "short": parts[1] if len(parts) > 1 else "",
                    "author": parts[2] if len(parts) > 2 else "",
                    "email": parts[3] if len(parts) > 3 else "",
                    "date": parts[4] if len(parts) > 4 else "",
                    "subject": parts[5] if len(parts) > 5 else "",
                    "body": (parts[6].strip() if len(parts) > 6 else ""),
                    "files": [], "diff": ""
                }
                # Changed files with status
                stat = subprocess.run(
                    ["git", "show", "--name-status", "--pretty=format:", chash],
                    cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace"
                ).stdout
                for line in stat.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    bits = line.split("\t")
                    if len(bits) >= 2:
                        info["files"].append({"status": bits[0][0], "name": bits[-1]})
                # Diff — cap size so a giant commit doesn't freeze the UI
                diff = subprocess.run(
                    ["git", "show", "--pretty=format:", "--no-color", chash],
                    cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace"
                ).stdout
                MAX = 200_000  # ~200 KB of diff text
                if len(diff) > MAX:
                    diff = diff[:MAX] + "\n\n… diff truncated (too large to display in full) …"
                info["diff"] = diff
                self.send_json(info)
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/browse":
            folder = ""
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
                folder = filedialog.askdirectory(title="Choose folder")
                root.destroy()
            except Exception:
                pass
            self.send_json({"path": folder})

        elif path == "/registervault":
            # Resolve the vault folder from the index.html path and build a manifest
            html_path = Path(unquote(qs.get("html", [""])[0]))
            folder    = html_path.parent if html_path.name == "index.html" else html_path
            try:
                if not folder.exists():
                    self.send_json({"error": "folder missing"})
                    return
                mf = folder / "manifest.json"
                if mf.exists():
                    m = json.loads(mf.read_text(encoding="utf-8"))
                else:
                    m = {"repo": folder.name}
                self.send_json({"path": str(folder), "manifest": m})
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/manifest":
            folder = Path(unquote(qs.get("path", [""])[0]))
            try:
                mf = folder / "manifest.json"
                if mf.exists():
                    data = json.loads(mf.read_text(encoding="utf-8"))
                else:
                    # Synthesize from whatever exists
                    data = {"repo": folder.name}
                    rel_idx = folder / "releases" / "index.json"
                    if rel_idx.exists():
                        rels = json.loads(rel_idx.read_text(encoding="utf-8"))
                        data["releases"] = len(rels)
                        if rels and rels[0].get("html_url"):
                            parts = rels[0]["html_url"].replace("https://github.com/","").split("/")
                            if len(parts) >= 2:
                                data["owner"] = parts[0]
                                data["repo"]  = parts[1]
                    mirror = folder / "_git_mirror"
                    if mirror.exists():
                        r = subprocess.run(["git","rev-list","--count","HEAD"],
                                           cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace")
                        try: data["commits"] = int(r.stdout.strip())
                        except: data["commits"] = 0
                    is_vault = any([
                        (folder / "_git_mirror").exists(),
                        (folder / "_working_copy").exists(),
                        (folder / "releases").exists(),
                        (folder / "index.html").exists(),
                        (folder / "manifest.json").exists(),
                        (folder / "HEAD").exists(),
                    ])
                    if not is_vault:
                        self.send_json({"error": "No DoomVault archive found in that folder. Select the repo folder itself (e.g. mainsailos\\\\), not its parent."})
                        return
                self.send_json(data)
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/vaultdata":
            folder = Path(unquote(qs.get("path", [""])[0]))
            try:
                rel_idx  = folder / "releases" / "index.json"
                releases = json.loads(rel_idx.read_text(encoding="utf-8")) if rel_idx.exists() else []
                mirror   = folder / "_git_mirror"
                if not mirror.exists() and (folder / "HEAD").exists():
                    mirror = folder
                commits = []
                if mirror.exists():
                    fmt_str = "%H%x00%h%x00%an%x00%ae%x00%ai%x00%s"
                    r = subprocess.run(
                        ["git", "log", f"--pretty=format:{fmt_str}", "-n99999", "HEAD"],
                        cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace"
                    )
                    for line in r.stdout.splitlines():
                        parts = line.split("\x00")
                        if len(parts) == 6:
                            commits.append({"hash":parts[0],"short":parts[1],"author":parts[2],
                                            "email":parts[3],"date":parts[4],"subject":parts[5]})
                self.send_json({"releases": releases, "commits": commits})
            except Exception as e:
                self.send_json({"error": str(e), "releases": [], "commits": []})

        elif path == "/open":
            p = unquote(qs.get("path", [""])[0])
            if p and Path(p).exists():
                webbrowser.open(f"file://{p}")
            self.send_response(200)
            self.end_headers()

        elif path == "/getassetmode":
            self.send_json({"mode": load_settings().get("asset_mode", "reveal")})

        elif path == "/gettaborder":
            self.send_json({"order": load_settings().get("tab_order", ["releases", "commits", "readme"])})

        elif path == "/estimate":
            repo_arg = unquote(qs.get("repo", [""])[0])
            token    = unquote(qs.get("token", [""])[0]) or None
            rellimit = int(qs.get("rellimit", ["0"])[0] or 0)
            relyear  = int(qs.get("relyear", ["0"])[0] or 0)
            want_assets    = qs.get("assets", ["1"])[0] == "1"
            want_snapshots = qs.get("snapshots", ["1"])[0] == "1"
            try:
                owner, repo = parse_repo(repo_arg)
                # Repo metadata gives git size (in KB)
                repo_bytes = 0
                try:
                    meta = gh_get(f"https://api.github.com/repos/{owner}/{repo}", token)
                    repo_bytes = int(meta.get("size", 0)) * 1024  # API reports KB
                except Exception:
                    pass
                # Releases
                rels = gh_paginate(f"https://api.github.com/repos/{owner}/{repo}/releases", token)
                if relyear and relyear > 0:
                    rels = [r for r in rels
                            if (r.get("published_at") or r.get("created_at") or "")[:4].isdigit()
                            and int((r.get("published_at") or r.get("created_at"))[:4]) >= relyear]
                if rellimit and rellimit > 0 and len(rels) > rellimit:
                    rels = rels[:rellimit]
                asset_bytes  = 0
                for r in rels:
                    if want_assets:
                        for a in r.get("assets", []):
                            asset_bytes += a.get("size", 0) or 0

                # Measure ONE real source tarball to anchor our size guesses,
                # instead of assuming a flat 5 MB. This is the key accuracy fix.
                sample_tarball = 0
                for r in rels:
                    if r.get("tarball_url"):
                        sample_tarball = measure_download_size(r["tarball_url"], token)
                        if sample_tarball > 0:
                            break
                # Fallback if measurement failed (rate limit / no releases)
                if sample_tarball <= 0:
                    # use repo size as a rough proxy for one compressed tree
                    sample_tarball = max(int(repo_bytes * 0.4), 3_000_000)

                # Source archives downloaded per release: zip + tar.gz, each ≈ one tarball
                source_bytes = 0
                for r in rels:
                    if r.get("zipball_url"): source_bytes += sample_tarball
                    if r.get("tarball_url"): source_bytes += sample_tarball
                total = repo_bytes + asset_bytes + source_bytes

                # Snapshot count: how many tags will be extracted (respects same filters)
                snapshot_count = 0
                tags_truncated = False
                if want_snapshots:
                    try:
                        all_tags = gh_paginate(f"https://api.github.com/repos/{owner}/{repo}/tags", token)
                        tags_truncated = getattr(gh_paginate, "last_truncated", False)
                        tag_total = len(all_tags)
                        if relyear and relyear > 0:
                            # tags API doesn't carry dates; approximate by filtered release count
                            snapshot_count = min(len(rels), tag_total) if rels else tag_total
                        else:
                            snapshot_count = tag_total
                        if rellimit and rellimit > 0:
                            snapshot_count = min(snapshot_count, rellimit)
                    except Exception:
                        snapshot_count = len(rels)
                        tags_truncated = True

                # ── Snapshot disk usage ──────────────────────────────────────
                # Each snapshot is a full EXTRACTED copy of the source tree.
                # An extracted tree is typically ~2.5x the compressed tarball.
                # Using the measured tarball makes this far more accurate than
                # guessing from the packed git size.
                one_tree_bytes = max(int(sample_tarball * 2.5), 10 * 1048576)
                snapshot_bytes = one_tree_bytes * snapshot_count

                # Git mirror on disk is typically larger than GitHub's reported
                # size (loose objects, no aggressive repack). Pad by 1.4x.
                git_disk_bytes = int(repo_bytes * 1.4)

                total = git_disk_bytes + asset_bytes + source_bytes + snapshot_bytes

                # Rough time estimate. Wall-clock heuristics:
                clone_s    = git_disk_bytes / (8 * 1048576) if git_disk_bytes else 5
                dl_s       = (asset_bytes + source_bytes) / (6 * 1048576)
                snap_s     = snapshot_count * 2.0   # extract + write a full tree
                est_seconds = int(clone_s + dl_s + snap_s + 5)

                note = "Asset sizes are exact from GitHub. Git repo, source archives, snapshot disk usage, and time are estimates and can vary."
                if want_snapshots and snapshot_count > 0:
                    note = ("Snapshots add the biggest chunk here: " + str(snapshot_count) +
                            " full copies of the source tree (~" + str(int(snapshot_bytes/1048576)) +
                            " MB total). Turn off Download Snapshots to skip them — the git history still keeps every version. " + note)
                if tags_truncated:
                    note = ("⚠ This repo has so many tags that the count was cut short by GitHub's rate limit, "
                            "so the real total is LARGER than shown. Add a GitHub Token to get the full count. " + note)
                if not want_assets:
                    note = "Assets are off. " + note

                self.send_json({
                    "total_bytes": total,
                    "repo_bytes": git_disk_bytes,
                    "asset_bytes": asset_bytes,
                    "asset_included": want_assets,
                    "source_bytes": source_bytes,
                    "snapshot_bytes": snapshot_bytes,
                    "release_count": len(rels),
                    "snapshot_count": snapshot_count,
                    "tags_truncated": tags_truncated,
                    "est_seconds": est_seconds,
                    "note": note,
                })
            except ValueError as e:
                self.send_json({"error": str(e)})
            except Exception as e:
                self.send_json({"error": "Could not estimate: " + str(e)})

        elif path == "/checkupdates":
            folder = Path(unquote(qs.get("path", [""])[0]))
            try:
                # Determine owner/repo from manifest, else from release html_url
                owner = repo = None
                mf = folder / "manifest.json"
                if mf.exists():
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    owner, repo = m.get("owner"), m.get("repo")
                rel_idx = folder / "releases" / "index.json"
                local_tags = set()
                newest_local_date = ""
                if rel_idx.exists():
                    local = json.loads(rel_idx.read_text(encoding="utf-8"))
                    local_tags = {r.get("tag_name") for r in local}
                    # find the most recent release we already have (by published/created date)
                    for r in local:
                        d = r.get("published_at") or r.get("created_at") or ""
                        if d > newest_local_date:
                            newest_local_date = d
                    if (not owner or not repo) and local and local[0].get("html_url"):
                        parts = local[0]["html_url"].replace("https://github.com/", "").split("/")
                        if len(parts) >= 2:
                            owner, repo = parts[0], parts[1]
                if not owner or not repo:
                    self.send_json({"error": "cannot determine repo"}); return
                # Fetch current releases from GitHub
                remote = gh_paginate(f"https://api.github.com/repos/{owner}/{repo}/releases", None)
                # "New" = a release we don't have AND published after our newest archived release.
                # If we have nothing archived yet, every release counts as new.
                new_tags = []
                for r in remote:
                    tag = r.get("tag_name")
                    if tag in local_tags:
                        continue
                    rdate = r.get("published_at") or r.get("created_at") or ""
                    if newest_local_date and rdate and rdate <= newest_local_date:
                        continue   # older than what we have — intentionally skipped, not "new"
                    new_tags.append(tag)
                self.send_json({
                    "new_count": len(new_tags),
                    "new_tags": new_tags[:50],
                    "owner": owner, "repo": repo,
                })
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/vaultfile":
            # Serve a file (e.g. README image) from the vault, by relative path.
            folder = Path(unquote(qs.get("path", [""])[0]))
            rel    = unquote(qs.get("rel", [""])[0]).replace("\\", "/").lstrip("/")
            # prevent path traversal
            if ".." in rel.split("/"):
                self.send_response(403); self.end_headers(); return
            try:
                data = None
                # look in working copy, then snapshots, then mirror HEAD
                working = folder / "_working_copy" / rel
                direct  = folder / rel
                for cand in (working, direct):
                    if cand.exists() and cand.is_file():
                        data = cand.read_bytes()
                        break
                if data is None:
                    mirror = folder / "_git_mirror"
                    if not mirror.exists() and (folder / "HEAD").exists():
                        mirror = folder
                    if mirror.exists():
                        r = subprocess.run(["git", "show", f"HEAD:{rel}"],
                                           cwd=mirror, capture_output=True)
                        if r.returncode == 0:
                            data = r.stdout
                if data is None:
                    self.send_response(404); self.end_headers(); return
                # guess content type from extension
                ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
                ctypes = {"png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg",
                          "gif":"image/gif","svg":"image/svg+xml","webp":"image/webp",
                          "ico":"image/x-icon","bmp":"image/bmp"}
                self.send_response(200)
                self.send_header("Content-Type", ctypes.get(ext, "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(500); self.end_headers()

        elif path == "/readme":
            folder = Path(unquote(qs.get("path", [""])[0]))
            try:
                content = None
                # 1) Look in the working copy (checked-out files)
                working = folder / "_working_copy"
                candidates = ["README.md", "README.MD", "Readme.md", "readme.md",
                              "README.markdown", "README.txt", "README", "README.rst"]
                search_dirs = [working, folder]
                for d in search_dirs:
                    if not d.exists():
                        continue
                    for name in candidates:
                        f = d / name
                        if f.exists() and f.is_file():
                            content = f.read_text(encoding="utf-8", errors="replace")
                            break
                    if content is not None:
                        break
                # 2) Fall back to extracting from the git mirror's HEAD
                if content is None:
                    mirror = folder / "_git_mirror"
                    if not mirror.exists() and (folder / "HEAD").exists():
                        mirror = folder
                    if mirror.exists():
                        listing = subprocess.run(
                            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                            cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace"
                        ).stdout.splitlines()
                        # find a top-level readme
                        readme_path = None
                        for line in listing:
                            base = line.split("/")[-1].lower()
                            if "/" not in line and base.startswith("readme"):
                                readme_path = line
                                break
                        if not readme_path:
                            for line in listing:
                                if line.split("/")[-1].lower().startswith("readme"):
                                    readme_path = line
                                    break
                        if readme_path:
                            content = subprocess.run(
                                ["git", "show", f"HEAD:{readme_path}"],
                                cwd=mirror, capture_output=True, text=True, encoding="utf-8", errors="replace"
                            ).stdout
                self.send_json({"content": content or ""})
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/setassetmode":
            mode = qs.get("mode", ["reveal"])[0]
            if mode not in ("reveal", "copy"):
                mode = "reveal"
            s = load_settings(); s["asset_mode"] = mode; save_settings(s)
            self.send_json({"ok": True, "mode": mode})

        elif path == "/revealasset":
            vault = unquote(qs.get("vault", [""])[0])
            rel   = unquote(qs.get("rel", [""])[0])
            target = (Path(vault) / rel)
            try:
                if not target.exists():
                    self.send_json({"error": f"File not found:\n{target}"}); return
                # Show the file in the OS file manager, highlighted
                if sys.platform == "win32":
                    subprocess.run(["explorer", "/select,", str(target)])
                elif sys.platform == "darwin":
                    subprocess.run(["open", "-R", str(target)])
                else:
                    # Linux: open the containing folder
                    subprocess.run(["xdg-open", str(target.parent)])
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/copyasset":
            vault = unquote(qs.get("vault", [""])[0])
            rel   = unquote(qs.get("rel", [""])[0])
            src_file = (Path(vault) / rel)
            try:
                if not src_file.exists():
                    self.send_json({"error": f"File not found:\n{src_file}"}); return
                downloads = Path.home() / "Downloads"
                downloads.mkdir(parents=True, exist_ok=True)
                dest = downloads / src_file.name
                # avoid clobbering: add (1), (2)… if needed
                if dest.exists():
                    stem, suf = dest.stem, dest.suffix
                    n = 1
                    while (downloads / f"{stem} ({n}){suf}").exists():
                        n += 1
                    dest = downloads / f"{stem} ({n}){suf}"
                shutil.copy2(src_file, dest)
                self.send_json({"ok": True, "dest": str(dest)})
            except Exception as e:
                self.send_json({"error": str(e)})

        elif path == "/cancel":
            global cancel_requested
            cancel_requested = True
            self.send_json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global log_lines, archive_running, cancel_requested
        from urllib.parse import urlparse
        path   = urlparse(self.path).path

        # sendBeacon('/goodbye') arrives as POST when the browser tab closes.
        # Handle it first, before reading/parsing any body.
        if path == "/goodbye":
            self.send_response(200)
            self.end_headers()
            print("\n☢  Browser tab closed — shutting down.", flush=True)
            threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        if path == "/vaults":
            save_vaults(body.get("vaults", []))
            self.send_json({"ok": True})

        elif path == "/settaborder":
            order = body.get("order", [])
            if isinstance(order, list) and order:
                s = load_settings(); s["tab_order"] = order; save_settings(s)
            self.send_json({"ok": True})

        elif path == "/start":
            if archive_running:
                self.send_json({"ok": False, "error": "Already running"})
                return
            try:
                owner, repo = parse_repo(body["repo"])
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)})
                return

            log_lines        = []
            cancel_requested = False
            archive_running  = True

            def worker():
                global archive_running
                try:
                    run_archive(
                        owner=owner, repo=repo, dest=body["dest"],
                        token=body.get("token") or None,
                        no_snapshots=not body.get("snapshots", True),
                        no_releases=not body.get("releases", True),
                        commit_limit=int(body.get("commits", 500)),
                        release_limit=int(body.get("rellimit", 0)),
                        release_year=int(body.get("relyear", 0)),
                        log=lambda k, m: log_lines.append([k, m]) or print(f"[{k}] {m}", flush=True),
                    )
                except Exception as e:
                    # Never let a worker error take down the process
                    log_lines.append(["ERROR", f"Unexpected error: {e}"])
                    print(f"[ERROR] Unexpected error: {e}", flush=True)
                finally:
                    archive_running = False

            threading.Thread(target=worker, daemon=True).start()
            self.send_json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

# ─── Git auto-installer ───────────────────────────────────────────────────────

def git_is_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

def ssl_download(url, dest_path):
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "DoomVault/1.0"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)

def install_git_windows():
    import tempfile
    FALLBACK = "https://github.com/git-for-windows/git/releases/download/v2.45.2.windows.1/Git-2.45.2-64-bit.exe"
    print("Git not found — downloading Git for Windows...")
    url = None
    try:
        import ssl, json as _j
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request("https://api.github.com/repos/git-for-windows/git/releases/latest",
                                     headers={"Accept":"application/vnd.github+json","User-Agent":"DoomVault/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            data = _j.loads(r.read())
        for a in data.get("assets", []):
            if a["name"].endswith(".exe") and "64-bit" in a["name"] and "Git-" in a["name"]:
                url = a["browser_download_url"]
                print(f"Latest: {a['name']}")
                break
    except Exception as e:
        print(f"API fetch failed ({e}), using fallback version.")
    url = url or FALLBACK
    tmp = Path(tempfile.gettempdir()) / "git-installer.exe"
    try:
        print("Downloading… (this may take a minute)")
        ssl_download(url, tmp)
    except Exception as e:
        print(f"Download failed: {e}"); return False
    print("Installing silently…")
    r = subprocess.run([str(tmp), "/VERYSILENT", "/NORESTART", "/NOCANCEL",
                        "/SUPPRESSMSGBOXES", "/COMPONENTS=gitlfs,assoc,assoc_sh"], capture_output=True)
    if r.returncode != 0:
        print(f"Installer exited {r.returncode}"); return False
    for p in [r"C:\Program Files\Git\cmd", r"C:\Program Files\Git\bin"]:
        if p not in os.environ.get("PATH", ""):
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
    return git_is_available()

def ensure_git():
    if git_is_available(): return
    if sys.platform == "win32":
        print("\n⚠  Git not found — installing automatically…")
        if install_git_windows():
            print("✓ Git installed!\n")
        else:
            print("✗ Install failed.\n  Get Git from: https://git-scm.com/download/win\n  Then restart DoomVault.")
            input("Press Enter to exit…")
            sys.exit(1)
    else:
        print("Git is not installed.\n  macOS: brew install git\n  Linux: sudo apt install git\nThen restart DoomVault.")
        input("Press Enter to exit…")
        sys.exit(1)

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_git()

    port   = find_free_port(7777)
    url    = f"http://127.0.0.1:{port}"

    # Threaded server — handles page load, pings, and file serving concurrently
    # so the first page load never queues behind another request.
    class ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = ThreadedServer(("127.0.0.1", port), Handler)

    print(f"☢  DoomVault on {url}")
    print("   Close the browser tab to quit.\n")

    # Watchdog — exits if the browser is really gone (tab closed).
    # Be tolerant: browsers throttle timers in background tabs, and long
    # downloads can delay pings, so we require a long gap before quitting.
    # The /goodbye beacon handles normal tab-close instantly; this watchdog
    # is only the backstop for a browser crash / force-kill.
    def watchdog():
        global browser_connected, last_ping
        while not browser_connected:   # wait for first connection
            time.sleep(1)
        misses = 0
        while True:
            time.sleep(5)
            # Never shut down while an archive is actively running — a
            # backgrounded/throttled tab during a long download is normal.
            if archive_running:
                misses = 0
                continue
            if time.time() - last_ping > 60:
                misses += 1
                # require 3 consecutive 60s windows (~3 min) with no ping
                if misses >= 3:
                    print("\n☢  Browser gone — shutting down.", flush=True)
                    os._exit(0)
            else:
                misses = 0
    threading.Thread(target=watchdog, daemon=True).start()

    # Run the server in a background thread so we control ordering cleanly
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Open browser once the server is actually accepting and answering requests
    def open_browser():
        import urllib.request as _u
        # wait until an HTTP GET actually returns, not just a socket connect
        for _ in range(300):
            try:
                with _u.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.3) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.1)
        webbrowser.open(url)
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n☢  DoomVault shut down.")
