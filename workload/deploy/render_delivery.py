"""Build opt-in, disjoint platform/migration/app ownership profiles."""

from copy import deepcopy
import argparse
import json
from pathlib import Path
import re

from render_ha import render as application
from render_database import render as database

DB_IMAGE = "ghcr.io/cloudnative-pg/postgresql:18.4@sha256:6138f19539304b585c6cafd1af82ca407f184139459a8e06f0880df4556d3588"


def render(*, release: dict, owner: str, namespace: str = "report-dev",
           storage_class: str, backend: str = "local", settings: dict | None = None) -> dict:
    if owner not in {"config-sync", "flux"}:
        raise ValueError("select one owner")
    if not {"schema-check", "cloud-queue-object-v1"}.issubset(release.get("capabilities", [])):
        raise ValueError("release must explicitly support migration/cloud contracts")
    if backend not in {"local", "gcp", "aws"}:
        raise ValueError("invalid backend")
    if backend == "gcp" and owner != "config-sync" or backend == "aws" and owner != "flux":
        raise ValueError("cloud and delivery owner mismatch")
    settings = settings or {}
    profile = application(image=release["image"], revision=release["source_revision"],
                          namespace=namespace)
    accounts = {i["metadata"]["name"]: i for i in profile["items"]
                if i["kind"] == "ServiceAccount"}
    if backend == "gcp":
        required = {"GCP_PROJECT", "PUBSUB_TOPIC", "PUBSUB_SUBSCRIPTION", "REPORT_BUCKET", "GCP_SERVICE_ACCOUNT"}
    elif backend == "aws":
        required = {"AWS_REGION", "SQS_QUEUE_URL", "REPORT_BUCKET", "AWS_ROLE_ARN"}
    else:
        required = set()
    if set(settings) != required:
        raise ValueError("supply exactly the nonsecret resource/identity settings")
    if any(not isinstance(v, str) or not v or len(v) > 256 for v in settings.values()):
        raise ValueError("invalid resource configuration")
    if backend == "aws":
        if settings["AWS_REGION"] != "eu-west-2" or not re.fullmatch(
                r"arn:aws:iam::[0-9]{12}:role/report-[a-zA-Z0-9-]{1,60}", settings["AWS_ROLE_ARN"]):
            raise ValueError("invalid London workload identity")
        queue = re.fullmatch(r"https://sqs\.eu-west-2\.amazonaws\.com/([0-9]{12})/report-[a-z0-9-]{1,60}",
                             settings["SQS_QUEUE_URL"])
        if not queue or queue[1] != settings["AWS_ROLE_ARN"].split(":")[4]:
            raise ValueError("queue/identity account mismatch")
    if backend == "gcp":
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", settings["GCP_PROJECT"]):
            raise ValueError("invalid GCP project")
        for name in ("PUBSUB_TOPIC", "PUBSUB_SUBSCRIPTION"):
            if not re.fullmatch(r"report-[a-z0-9-]{1,60}", settings[name]):
                raise ValueError("invalid PubSub resource")
        if not re.fullmatch(r"report-[a-z0-9-]+@" + re.escape(settings["GCP_PROJECT"]) +
                            r"\.iam\.gserviceaccount\.com", settings["GCP_SERVICE_ACCOUNT"]):
            raise ValueError("invalid workload identity")
        accounts["report-worker"]["metadata"]["annotations"][
            "iam.gke.io/gcp-service-account"] = settings["GCP_SERVICE_ACCOUNT"]
    if backend != "local" and not re.fullmatch(r"report-[a-z0-9-]{3,55}", settings["REPORT_BUCKET"]):
        raise ValueError("invalid report bucket")
    for obj in profile["items"]:
        if obj["kind"] != "Deployment" or obj["metadata"]["name"] != "report-worker":
            continue
        pod = obj["spec"]["template"]["spec"]
        container = pod["containers"][0]
        container["env"].append({"name": "WORK_BACKEND", "value": backend})
        container["env"] += [{"name": k, "value": v} for k, v in sorted(settings.items())
                             if k != "GCP_SERVICE_ACCOUNT"]
        if backend == "aws":
            container["env"] += [{"name": "AWS_WEB_IDENTITY_TOKEN_FILE", "value": "/var/run/workload-identity/token"},
                                 {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"}]
            container["volumeMounts"] = [{"name": "workload-identity", "mountPath": "/var/run/workload-identity", "readOnly": True}]
            pod["volumes"] = [{"name": "workload-identity", "projected": {"sources": [{"serviceAccountToken": {
                "audience": "sts.amazonaws.com", "expirationSeconds": 900, "path": "token"}}]}}]
    resources = []

    def resource(api, kind, name, spec=None, ns=namespace):
        obj = {"apiVersion": api, "kind": kind, "metadata": {"name": name}}
        if ns:
            obj["metadata"]["namespace"] = ns
        if spec is not None:
            obj["spec"] = spec
        resources.append(obj)
        return obj

    ns = resource("v1", "Namespace", namespace, ns=None)
    ns["metadata"]["labels"] = {"pod-security.kubernetes.io/enforce": "restricted"}
    resource("v1", "ResourceQuota", "lab-budget", {"hard": {
        "requests.cpu": "3", "requests.memory": "4Gi", "limits.cpu": "12", "limits.memory": "8Gi",
        "pods": "16", "persistentvolumeclaims": "3", "requests.storage": "30Gi"}})
    resource("networking.k8s.io/v1", "NetworkPolicy", "default-deny", {
        "podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
    apps = {"matchLabels": {"app.kubernetes.io/name": "report-workshop"}}
    db = {"matchLabels": {"cnpg.io/cluster": "report-db"}}
    resource("networking.k8s.io/v1", "NetworkPolicy", "workload-database", {
        "podSelector": apps, "policyTypes": ["Egress"], "egress": [
            {"to": [{"podSelector": db}], "ports": [{"protocol": "TCP", "port": 5432}]},
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
             "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]})
    # CNPG peer/control-plane policies require the qualified provider CNI/API
    # destinations. Leave these denied instead of emitting broad Internet access.
    resources += [deepcopy(account) for account in accounts.values()]
    resources.append(database(image=DB_IMAGE, storage_class=storage_class, namespace=namespace))
    role = resource("rbac.authorization.k8s.io/v1", "Role", "report-workload-writer")
    role["rules"] = [
        {"apiGroups": [""], "resources": ["configmaps", "services"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
        {"apiGroups": ["policy"], "resources": ["poddisruptionbudgets"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]}]
    binding = resource("rbac.authorization.k8s.io/v1", "RoleBinding", "report-workload-writer")
    binding["roleRef"] = {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role["metadata"]["name"]}
    if owner == "config-sync":
        binding["subjects"] = [{"kind": "ServiceAccount", "name": "ns-reconciler-" + namespace + "-report", "namespace": "config-management-system"}]
        resource("configsync.gke.io/v1beta1", "RepoSync", "report", {
            "sourceFormat": "unstructured", "sourceType": "git", "git": {
                "repo": "https://github.com/talisman36935org/gcp-platform-delivery-lab",
                "branch": "main", "dir": "profiles/delivery/apps", "auth": "none"}})
    else:
        resources.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
            "name": "report-reconciler", "namespace": "flux-system"}, "automountServiceAccountToken": False})
        binding["subjects"] = [{"kind": "ServiceAccount", "name": "report-reconciler", "namespace": "flux-system"}]
    app_objects = [i for i in profile["items"] if i["kind"] not in {"ServiceAccount", "Job"}]
    migrations = [i for i in profile["items"] if i["kind"] == "Job"]
    return {"platform": resources, "migrations": migrations, "apps": app_objects,
            "activation": "blocked: operator/CNI/cloud egress/identity/lifecycle qualification required"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--owner", choices=["config-sync", "flux"], required=True)
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--namespace", default="report-dev")
    parser.add_argument("--backend", choices=["local", "gcp", "aws"], default="local")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite an existing delivery tree")
    profile = render(release=json.loads(args.release.read_text()), owner=args.owner,
                     storage_class=args.storage_class, namespace=args.namespace,
                     backend=args.backend,
                     settings=json.loads(args.settings.read_text()) if args.settings else None)
    args.output.mkdir(parents=True)
    for group in (("platform", "apps") if args.owner == "config-sync" else
                  ("platform", "migrations", "apps")):
        directory = args.output / group
        directory.mkdir()
        objects = profile[group]
        if group == "apps" and args.owner == "config-sync":
            objects = objects + profile["migrations"]
        (directory / "resources.json").write_text(json.dumps(
            {"apiVersion": "v1", "kind": "List", "items": objects}, indent=2) + "\n")
        (directory / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            "resources:\n  - resources.json\n")
    if args.owner == "flux":
        graph = []
        previous = None
        for group in ("platform", "migrations", "apps"):
            spec = {"interval": "1m", "timeout": "5m", "prune": True, "wait": True,
                    "sourceRef": {"kind": "GitRepository", "name": "portfolio"},
                    "path": "./profiles/delivery/" + group}
            if previous:
                spec["dependsOn"] = [{"name": previous}]
                spec["serviceAccountName"] = "report-reconciler"
            if group == "platform":
                spec["healthCheckExprs"] = [{"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
                    "current": "has(status.readyInstances) && status.readyInstances == spec.instances"}]
            if group == "apps":
                spec["healthCheckExprs"] = [{
                    "apiVersion": "apps/v1", "kind": "Deployment",
                    "current": "has(status.observedGeneration) && has(status.updatedReplicas) "
                               "&& has(status.readyReplicas) && has(status.availableReplicas) "
                               "&& status.observedGeneration == metadata.generation "
                               "&& status.updatedReplicas == spec.replicas "
                               "&& status.readyReplicas == spec.replicas "
                               "&& status.availableReplicas == spec.replicas",
                }]
            name = "report-" + group
            graph.append({"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
                          "metadata": {"name": name, "namespace": "flux-system"}, "spec": spec})
            previous = name
        directory = args.output / "root"
        directory.mkdir()
        (directory / "resources.json").write_text(json.dumps(
            {"apiVersion": "v1", "kind": "List", "items": graph}, indent=2) + "\n")
        (directory / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - resources.json\n")
    (args.output / "activation.json").write_text(json.dumps({
        "status": profile["activation"], "owner": args.owner, "backend": args.backend,
        "release": json.loads(args.release.read_text())}, indent=2) + "\n")
    print("rendered blocked opt-in delivery profile; no cluster operation performed")


if __name__ == "__main__":
    main()
