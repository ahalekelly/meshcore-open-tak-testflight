# /// script
# requires-python = ">=3.12"
# dependencies = ["pyjwt[crypto]", "requests"]
# ///
"""Releases jade-on-mesh/meshcore-open-tak to TestFlight. A systemd timer on akelly-desktop runs it every 15 minutes.

Each run:
- opens a T3 thread about a failed TestFlight workflow run, or about this script failing for an hour;
- sends the newest processed build to the Community group and submits it for beta review once no other
  build is in review (Apple reviews one build per version at a time);
- starts a build of upstream main once main has been quiet for 8 hours, or of unchanged main when the newest
  upload is 60 days old (TestFlight builds expire after 90). A commit whose latest run failed waits for a
  manual run or a newer upstream commit.
"""

import base64
import json
import socket
import subprocess
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import requests

UPSTREAM = "jade-on-mesh/meshcore-open-tak"
REPO = "ahalekelly/meshcore-open-tak-testflight"
WORKFLOW = "testflight.yml"
APP_ID = "6829544641"
COMMUNITY_GROUP = "926b32b7-db68-465e-a346-0ca769c778bc"
KEY_ID, ISSUER = "ZT4M83BNFY", "1572e43c-4c24-4268-b52e-3f9460b87529"
KEY = Path.home() / f".appstoreconnect/AuthKey_{KEY_ID}.p8"
QUIET = timedelta(hours=8)
RENEW = timedelta(days=60)
CHECKOUT = Path(__file__).resolve().parent
STATE = Path.home() / ".local/state/meshcore-testflight.json"
IN_REVIEW = {"WAITING_FOR_BETA_REVIEW", "IN_BETA_REVIEW"}
NOT_NEWS = ("Merge pull request", "Run CI", "dart format")


def now():
    return datetime.now(timezone.utc)


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def asc(method, path, body=None):
    token = jwt.encode({"iss": ISSUER, "exp": int(time.time()) + 600, "aud": "appstoreconnect-v1"},
                       KEY.read_text(), algorithm="ES256", headers={"kid": KEY_ID})
    response = requests.request(method, "https://api.appstoreconnect.apple.com" + path, json=body, timeout=60,
                                headers={"Authorization": f"Bearer {token}"})
    if not response.ok:
        raise RuntimeError(f"App Store Connect {method} {path}: {response.status_code} {response.text}")
    return response.json() if response.text else None


def notify(title, body):
    subprocess.run([Path.home() / ".local/bin/uv", "run", "--quiet", Path.home() / ".agents/bin/t3-thread.py",
                    "new", CHECKOUT, title, "claudeAgent", "claude-opus-5-5", "/dev/stdin"],
                   input=body, text=True, check=True)


def workflow_runs():
    """TestFlight runs, newest first, each with the build number and commit from its name."""
    runs = gh("run", "list", "-R", REPO, "-w", WORKFLOW, "-L", "50",
              "--json", "databaseId,displayTitle,status,conclusion,url,createdAt,headSha")
    for run in runs:
        _, run["build"], run["sha"] = run["displayTitle"].split()
    return runs


def patches(run):
    """What each patch branch the run merged adds, from the comments in its patches.txt."""
    text = base64.b64decode(gh("api", f"repos/{REPO}/contents/patches.txt?ref={run['headSha']}")["content"]).decode()
    return [line.partition("#")[2].strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def whats_new(run, runs):
    previous = next((r for r in runs if r["conclusion"] == "success" and r["createdAt"] < run["createdAt"]), None)
    short = run["sha"][:7]
    fixes = patches(run)
    if previous and previous["sha"] == run["sha"] and patches(previous) == fixes:
        return f"Rebuild of {short}, unchanged, so the beta doesn't expire."
    lines = ["Not yet in upstream:", *[f"- {f}" for f in fixes]] if fixes else []
    if previous is None or previous["sha"] == run["sha"]:
        lines.append(f"Built from {UPSTREAM} at {short}.")
    else:
        commits = gh("api", f"repos/{UPSTREAM}/compare/{previous['sha']}...{run['sha']}")["commits"]
        subjects = [c["commit"]["message"].splitlines()[0].removesuffix(" [format]") for c in commits]
        lines += [f"Changes in {UPSTREAM} up to {short}:",
                  *[f"- {s}" for s in reversed(subjects) if not s.startswith(NOT_NEWS)]]
    return "\n".join(lines)[:4000]


def distribute(builds, runs):
    """Send the newest build to testers once it has processed and no other build is in review."""
    build = builds["data"][0]
    attributes = build["attributes"]
    detail = next(i for i in builds["included"] if i["type"] == "buildBetaDetails" and i["id"] == build["id"])
    if attributes["processingState"] != "VALID" or attributes["expired"]:
        return
    if attributes["usesNonExemptEncryption"] is None:
        asc("PATCH", f"/v1/builds/{build['id']}",
            {"data": {"type": "builds", "id": build["id"], "attributes": {"usesNonExemptEncryption": False}}})
        return  # externalBuildState updates on a later run
    states = {i["attributes"]["externalBuildState"] for i in builds["included"] if i["type"] == "buildBetaDetails"}
    if detail["attributes"]["externalBuildState"] != "READY_FOR_BETA_SUBMISSION" or states & IN_REVIEW:
        return
    run = next(r for r in runs if r["build"] == attributes["version"] and r["conclusion"] == "success")
    text = whats_new(run, runs)
    localizations = asc("GET", f"/v1/builds/{build['id']}/betaBuildLocalizations")["data"]
    if localizations:
        asc("PATCH", f"/v1/betaBuildLocalizations/{localizations[0]['id']}",
            {"data": {"type": "betaBuildLocalizations", "id": localizations[0]["id"],
                      "attributes": {"whatsNew": text}}})
    else:
        asc("POST", "/v1/betaBuildLocalizations",
            {"data": {"type": "betaBuildLocalizations", "attributes": {"locale": "en-US", "whatsNew": text},
                      "relationships": {"build": {"data": {"type": "builds", "id": build["id"]}}}}})
    asc("POST", f"/v1/betaGroups/{COMMUNITY_GROUP}/relationships/builds",
        {"data": [{"type": "builds", "id": build["id"]}]})
    asc("POST", "/v1/betaAppReviewSubmissions",
        {"data": {"type": "betaAppReviewSubmissions",
                  "relationships": {"build": {"data": {"type": "builds", "id": build["id"]}}}}})


def release(state):
    runs = workflow_runs()
    if runs and runs[0]["conclusion"] == "failure" and state.get("reported") != runs[0]["databaseId"]:
        run = runs[0]
        notify(f"MeshCore TestFlight build {run['build']} failed",
               f"The TestFlight workflow run {run['url']} failed, building {UPSTREAM} at {run['sha']}.\n"
               f"Read the log with `gh run view --log-failed -R {REPO} {run['databaseId']}`, and work in "
               f"{CHECKOUT} (see its README). Decide whether the cause is upstream, one of our patch branches "
               "(patches.txt), or this repo. Fix our causes and start a new run; the scheduler skips this commit "
               "until a run of it succeeds or upstream moves. Patch branches are heads of open upstream PRs, so "
               "get Adrian's approval before pushing to them. Report upstream causes, and draft any upstream "
               "issue or PR for Adrian to approve.")
        state["reported"] = run["databaseId"]
    builds = asc("GET", f"/v1/builds?filter[app]={APP_ID}&sort=-uploadedDate&limit=10&include=buildBetaDetail"
                        "&fields[builds]=version,uploadedDate,processingState,usesNonExemptEncryption,expired,"
                        "buildBetaDetail")
    distribute(builds, runs)
    if any(r["status"] != "completed" for r in runs):
        return
    head = gh("api", f"repos/{UPSTREAM}/commits/main")
    sha = head["sha"]
    if runs and runs[0]["sha"] == sha and runs[0]["conclusion"] == "failure":
        return
    built = next((r["sha"] for r in runs if r["conclusion"] == "success"), None)
    quiet = now() - datetime.fromisoformat(head["commit"]["committer"]["date"]) >= QUIET
    expiring = now() - datetime.fromisoformat(builds["data"][0]["attributes"]["uploadedDate"]) >= RENEW
    if sha != built and quiet or expiring:
        gh("workflow", "run", WORKFLOW, "-R", REPO, "-f", f"sha={sha}",
           "-f", f"build={now().strftime('%Y%m%d%H')}")


def main():
    if socket.gethostname() != "akelly-desktop":
        raise SystemExit("scheduler.py runs only on akelly-desktop")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    try:
        release(state)
        state.pop("failure", None)
    except Exception:
        error = traceback.format_exc()
        failure = state.setdefault("failure", {"since": now().isoformat(), "notified": None})
        notified = failure["notified"] and now() - datetime.fromisoformat(failure["notified"]) < timedelta(days=1)
        if now() - datetime.fromisoformat(failure["since"]) >= timedelta(hours=1) and not notified:
            notify("MeshCore TestFlight scheduler failing",
                   f"{CHECKOUT / 'scheduler.py'} has failed since {failure['since']}. Latest error:\n\n{error}\n"
                   "Logs: `journalctl --user -u meshcore-testflight`. Fix the cause; if the fix needs Adrian's "
                   "decision, explain the problem and the options.")
            failure["notified"] = now().isoformat()
        raise
    finally:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2) + "\n")


if __name__ == "__main__":
    main()
