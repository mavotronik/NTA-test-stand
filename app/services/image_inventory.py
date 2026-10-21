from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def is_stand_image_tag(tag: str) -> bool:
    t = (tag or "").strip()
    if not t:
        return False
    if t.startswith("grapes-nta-test:"):
        return True
    if t == "grapes-nta:latest" or t.startswith("grapes-nta:"):
        return True
    return False


def normalize_image_id(image_id: str) -> str:
    iid = (image_id or "").strip()
    if not iid:
        return ""
    if not iid.startswith("sha256:"):
        if len(iid) == 64 and all(c in "0123456789abcdef" for c in iid):
            return f"sha256:{iid}"
    return iid


def _short_id(image_id: str) -> str:
    iid = normalize_image_id(image_id)
    if iid.startswith("sha256:"):
        return iid[7:19]
    return iid[:12]


def _format_ts(ts: int | float | str | None) -> str:
    if ts is None:
        return "—"
    try:
        if isinstance(ts, str):
            return ts
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OSError):
        return "—"


def _format_size(size: int | None) -> str:
    if size is None or size <= 0:
        return "—"
    units = ["B", "KiB", "MiB", "GiB"]
    val = float(size)
    for unit in units:
        if val < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(val)} B"
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{size} B"


@dataclass
class ImageUsageRef:
    kind: str
    label: str
    detail: str
    href: str | None
    role: str | None
    status: str


@dataclass
class StandImageRow:
    image_id: str
    short_id: str
    tags: list[str]
    size: int
    size_label: str
    created: str
    in_use: bool
    in_use_now: bool
    deletable: bool
    usages: list[ImageUsageRef] = field(default_factory=list)
    build_id: str | None = None
    git_commit: str | None = None


def _tag_set_for_row(tags: list[str]) -> set[str]:
    return {(t or "").strip() for t in tags if (t or "").strip()}


def _instance_usages(
    instances: list[dict[str, Any]],
    tag_set: set[str],
) -> list[ImageUsageRef]:
    out: list[ImageUsageRef] = []
    for inst in instances:
        iid = inst["id"]
        name = inst.get("name") or inst.get("slug") or iid
        slug = inst.get("slug") or ""
        state = inst.get("state") or ""
        for field_name, role in (("image_tag", "current"), ("rollback_image_tag", "rollback")):
            tag = (inst.get(field_name) or "").strip()
            if not tag:
                continue
            if tag not in tag_set:
                continue
            out.append(
                ImageUsageRef(
                    kind="instance",
                    label=name,
                    detail=f"{slug} · {state}" if slug else state,
                    href=f"/instances/{iid}",
                    role=role,
                    status=state,
                )
            )
    return out


def _container_usages(
    containers: list[dict[str, Any]],
    image_id: str,
    tag_set: set[str],
) -> list[ImageUsageRef]:
    out: list[ImageUsageRef] = []
    norm_id = normalize_image_id(image_id)
    for c in containers:
        c_img = normalize_image_id(c.get("image_id") or "")
        c_tag = (c.get("image") or "").strip()
        match = False
        if norm_id and c_img and c_img == norm_id:
            match = True
        elif c_tag and c_tag in tag_set:
            match = True
        if not match:
            continue
        out.append(
            ImageUsageRef(
                kind="container",
                label=c.get("name") or c.get("id") or "container",
                detail=c.get("status") or "",
                href=None,
                role=None,
                status=c.get("status") or "",
            )
        )
    return out


def compute_in_use_now(
    usages: list[ImageUsageRef],
) -> bool:
    for u in usages:
        if u.kind == "container" and u.status == "running":
            return True
        if u.kind == "instance" and u.role == "current" and u.status in ("running", "deploying"):
            return True
    return False


def merge_stand_image_rows(
    docker_images: list[dict[str, Any]],
    instances: list[dict[str, Any]],
    containers: list[dict[str, Any]],
    builds: list[dict[str, Any]],
) -> list[StandImageRow]:
    by_id: dict[str, dict[str, Any]] = {}
    build_by_id: dict[str, dict[str, Any]] = {}
    build_by_tag: dict[str, dict[str, Any]] = {}
    for b in builds:
        if (b.get("status") or "") == "removed":
            continue
        bid = b.get("id")
        if bid:
            build_by_id[bid] = b
        tag = (b.get("image_tag") or "").strip()
        if tag:
            build_by_tag[tag] = b
        iid = normalize_image_id(b.get("image_id") or "")
        if iid:
            by_id.setdefault(iid, {"tags": set(), "size": 0, "created": None})
            by_id[iid].setdefault("build", b)

    for img in docker_images:
        iid = normalize_image_id(img.get("id") or "")
        if not iid:
            continue
        tags = [t for t in (img.get("tags") or []) if is_stand_image_tag(t)]
        if not tags and iid not in by_id:
            continue
        row = by_id.setdefault(iid, {"tags": set(), "size": 0, "created": None})
        row["tags"].update(tags)
        if img.get("size"):
            row["size"] = int(img["size"])
        if img.get("created") is not None:
            row["created"] = img.get("created")

    for b in builds:
        if (b.get("status") or "") == "removed":
            continue
        iid = normalize_image_id(b.get("image_id") or "")
        tag = (b.get("image_tag") or "").strip()
        if not iid:
            continue
        if iid in by_id:
            continue
        if tag and is_stand_image_tag(tag):
            by_id[iid] = {"tags": {tag}, "size": 0, "created": b.get("created_at"), "build": b}

    rows: list[StandImageRow] = []
    for iid, meta in by_id.items():
        tags = sorted(meta.get("tags") or [])
        tag_set = _tag_set_for_row(tags)
        inst_refs = _instance_usages(instances, tag_set)
        cont_refs = _container_usages(containers, iid, tag_set)
        usages = inst_refs + cont_refs
        in_use = bool(usages)
        in_use_now = compute_in_use_now(usages)
        build = meta.get("build")
        if not build:
            for t in tags:
                if t in build_by_tag:
                    build = build_by_tag[t]
                    break
        rows.append(
            StandImageRow(
                image_id=iid,
                short_id=_short_id(iid),
                tags=tags,
                size=int(meta.get("size") or 0),
                size_label=_format_size(meta.get("size")),
                created=_format_ts(meta.get("created")),
                in_use=in_use,
                in_use_now=in_use_now,
                deletable=not in_use,
                usages=usages,
                build_id=build.get("id") if build else None,
                git_commit=(build.get("git_commit") or "")[:12] if build else None,
            )
        )
    rows.sort(key=lambda r: (r.created or "", r.short_id), reverse=True)
    return rows


def image_row_rev(row: StandImageRow) -> str:
    parts = [
        row.short_id,
        ",".join(row.tags),
        str(row.in_use),
        str(row.in_use_now),
        str(len(row.usages)),
    ]
    return "|".join(parts)
