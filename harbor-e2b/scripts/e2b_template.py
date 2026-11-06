#!/usr/bin/env python3
"""Plan, prebuild, probe and retire one Harbor/E2B template. Secrets use env only."""
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "harbor-e2b-template/1"
SKILL_ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, str):
        for key in ("E2B_API_KEY", "REGISTRY_PASSWORD"):
            secret = os.getenv(key)
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def emit(value):
    print(json.dumps(clean(value), ensure_ascii=False, indent=2), flush=True)


def output_path(value):
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(SKILL_ROOT):
        raise ValueError("Keep receipts and runtime data outside the installed skill")
    return path


def save(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            handle.write(json.dumps(clean(data), ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def connection():
    endpoint = os.environ["E2B_API_URL"].rstrip("/")
    domain = os.environ["E2B_DOMAIN"]
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path
            or parsed.hostname != "api." + domain or parsed.port not in (None, 443)):
        raise ValueError("E2B_API_URL must be https://api.<E2B_DOMAIN> for the selected endpoint")
    key = os.environ["E2B_API_KEY"]
    if not key:
        raise ValueError("Empty E2B_API_KEY")
    return {"api_url": endpoint, "domain": domain, "api_key": key, "request_timeout": 45}


def image_host(image):
    if "://" in image or any(c.isspace() for c in image):
        raise ValueError("Use an OCI image reference without URL credentials or whitespace")
    if "@" in image and not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError("Image @ reference must be a complete sha256 digest")
    first = image.split("/", 1)[0]
    return first if "/" in image and ("." in first or ":" in first or first == "localhost") else "docker.io"


def registry_auth(image, mode):
    if mode == "micro":
        return {}  # Never forward an unrelated registry account to an official ACR image.
    user, password = os.getenv("REGISTRY_USERNAME"), os.getenv("REGISTRY_PASSWORD")
    if bool(user) != bool(password):
        raise ValueError("Set both REGISTRY_USERNAME and REGISTRY_PASSWORD")
    if not user:
        return {}
    if os.getenv("REGISTRY_HOST") != image_host(image):
        raise ValueError("REGISTRY_HOST must exactly match the build image host before sharing credentials")
    return {"username": user, "password": password}


def make_plan(task_path, image=None, mode="default"):
    from harbor.models.task.task import Task
    from harbor.environments.definition import environment_content_hash, SNAPSHOT_HASH_LEN

    task = Task(Path(task_path).expanduser().resolve())
    cfg = task.config
    env = cfg.environment
    if cfg.steps or cfg.verifier.environment is not None or str(cfg.verifier.environment_mode) in {"separate", "VerifierEnvironmentMode.SEPARATE"}:
        raise ValueError("This helper handles single-step tasks with a shared verifier only")
    if env.gpus or env.tpu or str(env.os) not in {"linux", "TaskOS.LINUX"}:
        raise ValueError("This helper handles CPU Linux tasks only")
    for filename in ("docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"):
        if (task.paths.environment_dir / filename).exists():
            raise ValueError("Compose requires a separate service/template plan")
    for file in task.paths.environment_dir.rglob("*"):
        if file.is_symlink():
            raise ValueError("Resolve environment symlinks in a reviewed source snapshot first")
    image = image or env.docker_image
    if not image:
        raise ValueError("Provide a fully built --image; this helper does not execute Dockerfile RUN/COPY")
    host = image_host(image)
    if mode == "micro" and not host.endswith(".cr.aliyuncs.com"):
        raise ValueError("The supported micro path requires an explicitly selected ACR image")
    if any(type(v) is not int or v <= 0 for v in (env.cpus, env.memory_mb, env.storage_mb)):
        raise ValueError("Task must resolve positive CPU, memory_mb and storage_mb requirements")
    env_hash = environment_content_hash(task.paths.environment_dir, docker_image=env.docker_image)
    alias = f"{task.short_name}__{env_hash[:SNAPSHOT_HASH_LEN]}".replace("/", "__").replace(".", "-")
    return {
        "task_path": str(task.paths.task_dir), "task_name": task.name,
        "task_toml_sha256": hashlib.sha256((task.paths.task_dir / "task.toml").read_bytes()).hexdigest(),
        "environment_hash": env_hash, "alias": alias, "image": image,
        "declared_image": env.docker_image, "image_pinned": "@sha256:" in image,
        "image_equivalence": "NOT_CHECKED_BY_THIS_HELPER",
        "mode": mode, "cpus": env.cpus, "memory_mb": env.memory_mb,
        "storage_mb": env.storage_mb, "build_timeout_sec": env.build_timeout_sec,
        "harbor_version": importlib.metadata.version("harbor"),
        "e2b_version": importlib.metadata.version("e2b"),
    }


def templates(client):
    response = client.get("/templates")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected template list; do not infer that the alias is absent")
    return data


def check_identity(receipt, remote):
    if (not remote or remote.get("templateID") != receipt.get("template_id")
            or remote.get("buildID") != receipt.get("build_id")
            or receipt["plan"]["alias"] not in remote.get("aliases", [])):
        raise ValueError("Remote template identity differs from the recorded build")


def resource_differences(plan, remote, accepted_disk=None):
    expected = {"cpuCount": plan["cpus"], "memoryMB": plan["memory_mb"],
                "diskSizeMB": accepted_disk if accepted_disk is not None else plan["storage_mb"]}
    return {key: {"expected": value, "actual": remote.get(key)}
            for key, value in expected.items() if remote.get(key) != value}


def read_receipt(path, conn):
    receipt = json.loads(path.read_text())
    if receipt.get("schema") != SCHEMA or receipt.get("endpoint") != conn["api_url"] or receipt.get("domain") != conn["domain"]:
        raise ValueError("Receipt schema or endpoint mismatch")
    return receipt


def build(args, conn, client):
    from e2b import Template

    plan = make_plan(args.task, args.image, args.mode)
    auth = registry_auth(plan["image"], plan["mode"])
    if plan["mode"] == "micro":
        region = conn["domain"].split(".", 1)[0]
        if f".{region}.cr.aliyuncs.com" not in image_host(plan["image"]):
            raise ValueError("ACR image region differs from the configured sandbox region")
    path = output_path(args.receipt)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        handle.write("{}\n")
    receipt = {"schema": SCHEMA, "run_id": uuid.uuid4().hex, "started_at": now(),
               "endpoint": conn["api_url"], "domain": conn["domain"], "plan": plan,
               "owned": False, "status": "PLANNED", "registry_auth_supplied": bool(auth)}
    save(path, receipt)
    try:
        if any(plan["alias"] in item.get("aliases", []) for item in templates(client)):
            raise ValueError("Alias already exists; use its trusted receipt to probe, or inspect it first")
        receipt["status"] = "BUILD_REQUESTED"
        save(path, receipt)
        headers = ({"X-E2B-Template-Build-Mode": "micro",
                    "X-E2B-Template-Alpha-Disk-Size": str(plan["storage_mb"])}
                   if plan["mode"] == "micro" else {})
        result = Template.build_in_background(
            Template().from_image(plan["image"], **auth), name=plan["alias"],
            cpu_count=plan["cpus"], memory_mb=plan["memory_mb"], api_headers=headers, **conn)
        receipt.update(owned=True, template_id=result.template_id, build_id=result.build_id, status="BUILDING")
        save(path, receipt)
        deadline = time.monotonic() + plan["build_timeout_sec"]
        while time.monotonic() < deadline:
            response = client.get(f"/templates/{result.template_id}/builds/{result.build_id}/status")
            response.raise_for_status()
            state = response.json()
            receipt["build_status"] = state
            save(path, receipt)
            if state.get("status") == "error":
                receipt["status"] = "BUILD_FAILED"
                save(path, receipt)
                return 1
            if state.get("status") == "ready":
                remote = next((item for item in templates(client) if item.get("templateID") == result.template_id), None)
                check_identity(receipt, remote)
                receipt["resource_readback"] = remote
                receipt["resource_differences"] = resource_differences(plan, remote)
                receipt["status"] = "RESOURCE_MISMATCH" if receipt["resource_differences"] else "READY"
                save(path, receipt)
                return int(bool(receipt["resource_differences"]))
            if state.get("status") not in {"building", "waiting"}:
                raise ValueError("Unknown build state; preserve receipt and inspect the provider")
            time.sleep(3)
        receipt["status"] = "BUILD_TIMEOUT"
        save(path, receipt)
        return 1
    except BaseException as error:
        receipt.update(status="ERROR", error_type=type(error).__name__, error=str(error),
                       ownership_uncertain=not receipt["owned"] and receipt["status"] == "BUILD_REQUESTED")
        save(path, receipt)
        raise


def probe(args, conn, client):
    from e2b import Sandbox

    path = output_path(args.receipt)
    receipt = read_receipt(path, conn)
    plan = receipt["plan"]
    current = make_plan(args.task, plan["image"], plan["mode"])
    binding = lambda item: {key: value for key, value in item.items() if key != "task_path"}
    if binding(current) != binding(plan):
        raise ValueError("Task, environment, resource or tool version binding changed")
    receipt["last_probe_task_path"] = current["task_path"]
    remote = next((item for item in templates(client) if item.get("templateID") == receipt.get("template_id")), None)
    check_identity(receipt, remote)
    if remote.get("buildStatus") != "ready":
        raise ValueError("Remote template is not ready")
    differences = resource_differences(plan, remote, args.accept_disk_mb)
    receipt.update(resource_readback=remote, resource_differences=differences)
    if args.accept_disk_mb is not None:
        if args.accept_disk_mb <= 0:
            raise ValueError("Accepted disk size must be positive")
        receipt["accepted_disk_mb"] = args.accept_disk_mb
    if differences:
        receipt["status"] = "RESOURCE_MISMATCH"
        save(path, receipt)
        return 1
    run = {"started_at": now(), "cleanup_confirmed": False}
    receipt.setdefault("probes", []).append(run)
    sandbox = None
    try:
        sandbox = Sandbox.create(template=receipt["template_id"], timeout=180,
                                 metadata={"harbor_e2b_probe": receipt["run_id"]}, **conn)
        run["sandbox_id"] = sandbox.sandbox_id
        save(path, receipt)
        result = sandbox.commands.run("printf 'HARBOR_E2B_READY\\n'; id; command -v bash", user="root", timeout=30)
        run.update(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)
        if result.exit_code != 0 or "HARBOR_E2B_READY" not in result.stdout:
            raise RuntimeError("Sandbox command probe failed")
        receipt["status"] = "PROBED"
    except BaseException as error:
        run.update(error_type=type(error).__name__, error=str(error))
        receipt["status"] = "PROBE_ERROR"
        raise
    finally:
        if sandbox is not None:
            try:
                run["kill_result"] = sandbox.kill()
                response = client.get("/sandboxes")
                response.raise_for_status()
                active = response.json()
                if not isinstance(active, list):
                    raise ValueError("Unexpected sandbox list response")
                if any(not isinstance(item, dict) or not isinstance(item.get("sandboxID", item.get("sandbox_id")), str) for item in active):
                    raise ValueError("Sandbox identifiers missing; cleanup cannot be confirmed")
                run["cleanup_confirmed"] = not any(item.get("sandboxID", item.get("sandbox_id")) == sandbox.sandbox_id for item in active)
                if not run["cleanup_confirmed"]:
                    receipt["status"] = "CLEANUP_UNCONFIRMED"
            except Exception as error:
                run["cleanup_error"] = str(error)
                receipt["status"] = "CLEANUP_UNCONFIRMED"
        run["finished_at"] = now()
        save(path, receipt)
    return 0 if receipt["status"] == "PROBED" and run["cleanup_confirmed"] else 1


def delete_template(args, conn, client):
    path = output_path(args.receipt)
    receipt = read_receipt(path, conn)
    if receipt.get("owned") is not True or not receipt.get("template_id") or not receipt.get("build_id"):
        raise ValueError("No confirmed creation receipt; refuse to delete an unowned template")
    if any(p.get("sandbox_id") and not p.get("cleanup_confirmed") for p in receipt.get("probes", [])):
        raise ValueError("Probe sandbox cleanup is unresolved; reconcile it before deleting the template")
    remote = next((item for item in templates(client) if item.get("templateID") == receipt["template_id"]), None)
    if remote is not None:
        check_identity(receipt, remote)
        response = client.delete("/templates/" + receipt["template_id"])
        response.raise_for_status()
    for _ in range(10):
        if not any(item.get("templateID") == receipt["template_id"] for item in templates(client)):
            receipt.update(status="DELETED", deleted_at=now(), deletion_confirmed=True)
            save(path, receipt)
            return 0
        time.sleep(1)
    receipt.update(deletion_confirmed=False, status="DELETE_UNCONFIRMED")
    save(path, receipt)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build", "probe", "delete-template"):
        sub = commands.add_parser(name)
        if name != "delete-template":
            sub.add_argument("--task", required=True)
        if name in {"plan", "build"}:
            sub.add_argument("--image")
            sub.add_argument("--mode", choices=["default", "micro"], default="default")
        if name != "plan":
            sub.add_argument("--receipt", required=True)
        if name == "probe":
            sub.add_argument("--accept-disk-mb", type=int)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            emit(make_plan(args.task, args.image, args.mode))
            return 0
        import httpx
        conn = connection()
        with httpx.Client(base_url=conn["api_url"], headers={"X-API-Key": conn["api_key"]}, timeout=45) as client:
            action = {"build": build, "probe": probe, "delete-template": delete_template}[args.command]
            code = action(args, conn, client)
        receipt = json.loads(output_path(args.receipt).read_text())
        emit({"status": receipt["status"], "receipt": str(output_path(args.receipt)),
              "template_id": receipt.get("template_id"), "resource_differences": receipt.get("resource_differences", {})})
        return code
    except Exception as error:
        emit({"status": "ERROR", "error_type": type(error).__name__, "message": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
