"""Build, push, and (re)create the self-hosted Daytona snapshot for SANDBOX_TYPE=daytona.

A single invocation does all three steps:
  1. docker build the sandbox image from sandbox-image/open-swe-sandbox.Dockerfile
  2. docker push it to the local registry
  3. delete + recreate the immutable Daytona snapshot pointing at it

--image is the *runner-facing* reference (the daytona-runner resolves the registry by
its in-network name, e.g. registry:6000). The host can't resolve that name, so the
build/push use the host-facing alias of the same registry (--push-host, default
localhost:6000), derived automatically from --image.

Usage:
    uv run python scripts/create_daytona_snapshot.py \
        --name open-swe-sandbox \
        --image registry:6000/open-swe-sandbox:0.1.2

    # only recreate the snapshot from an already-pushed image:
    uv run python scripts/create_daytona_snapshot.py --image registry:6000/open-swe-sandbox:0.1.2 --skip-build
"""

import argparse
import os
import subprocess
import sys
import time
import urllib.request

from daytona import (
    CreateSnapshotParams,
    Daytona,
    DaytonaConfig,
    DaytonaConflictError,
    DaytonaNotFoundError,
    Resources,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DOCKERFILE = os.path.join(_REPO_ROOT, "sandbox-image", "open-swe-sandbox.Dockerfile")
_DEFAULT_CONTEXT = os.path.join(_REPO_ROOT, "sandbox-image")


def _split_registry(image: str) -> tuple[str | None, str]:
    """Split 'registry:6000/repo:tag' into ('registry:6000', 'repo:tag')."""
    first, sep, rest = image.partition("/")
    if sep and (":" in first or "." in first or first == "localhost"):
        return first, rest
    return None, image


def _run(cmd: list[str]) -> None:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def _verify_pushed(push_host: str, repo_and_tag: str) -> None:
    repo, _, tag = repo_and_tag.rpartition(":")
    try:
        with urllib.request.urlopen(f"http://{push_host}/v2/{repo}/tags/list", timeout=5) as r:
            body = r.read().decode()
        if tag and f'"{tag}"' not in body:
            print(f"WARNING: tag '{tag}' not found in registry response: {body}", file=sys.stderr)
        else:
            print(f"Verified registry has {repo}:{tag}")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not verify registry tag ({type(e).__name__}: {e})", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build, push, and (re)create a Daytona snapshot")
    parser.add_argument("--name", default=os.getenv("DAYTONA_SANDBOX_SNAPSHOT", "open-swe-sandbox"))
    parser.add_argument("--image", required=True, help="runner-facing ref, e.g. registry:6000/open-swe-sandbox:0.1.2")
    parser.add_argument("--push-host", default=os.getenv("DAYTONA_PUSH_HOST", "localhost:6000"),
                        help="host-facing alias of the runner registry, used for build/push (default localhost:6000)")
    parser.add_argument("--dockerfile", default=_DEFAULT_DOCKERFILE)
    parser.add_argument("--context", default=_DEFAULT_CONTEXT)
    parser.add_argument("--skip-build", action="store_true", help="skip docker build+push; only recreate the snapshot")
    parser.add_argument("--cpu", type=int, default=int(os.getenv("DAYTONA_SNAPSHOT_CPU", "1")))
    parser.add_argument("--memory", type=int, default=int(os.getenv("DAYTONA_SNAPSHOT_MEM_GB", "1")))
    parser.add_argument("--disk", type=int, default=int(os.getenv("DAYTONA_SNAPSHOT_DISK_GB", "3")))
    args = parser.parse_args()

    runner_registry, repo_and_tag = _split_registry(args.image)
    push_ref = f"{args.push_host}/{repo_and_tag}" if runner_registry else args.image

    if not args.skip_build:
        _run(["docker", "build", "-t", push_ref, "-f", args.dockerfile, args.context])
        _run(["docker", "push", push_ref])
        _verify_pushed(args.push_host, repo_and_tag)

    daytona = Daytona(
        config=DaytonaConfig(
            api_key=os.environ["DAYTONA_API_KEY"],
            api_url=os.getenv("DAYTONA_API_URL", "http://localhost:3000/api"),
        )
    )

    # Snapshots are immutable: delete any existing one with this name, then recreate.
    # delete() returns before Daytona finishes removing it, so poll until it's gone.
    try:
        existing = daytona.snapshot.get(args.name)
        print(f"\nDeleting existing snapshot '{args.name}'...")
        daytona.snapshot.delete(existing)
        for _ in range(30):
            try:
                daytona.snapshot.get(args.name)
                time.sleep(1)
            except DaytonaNotFoundError:
                break
        else:
            print("WARNING: snapshot still present after delete wait; continuing anyway.")
    except DaytonaNotFoundError:
        print(f"\nNo existing snapshot named '{args.name}' to delete; continuing.")

    print(f"Creating snapshot '{args.name}' from image '{args.image}'...")
    params = CreateSnapshotParams(
        name=args.name,
        image=args.image,
        resources=Resources(cpu=args.cpu, memory=args.memory, disk=args.disk),
    )
    for attempt in range(10):
        try:
            snapshot = daytona.snapshot.create(params, on_logs=print)
            break
        except DaytonaConflictError:
            if attempt == 9:
                raise
            time.sleep(2)
    print(f"Snapshot ready: name={args.name} id={getattr(snapshot, 'id', '?')}")


if __name__ == "__main__":
    main()