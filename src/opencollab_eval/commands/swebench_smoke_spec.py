"""Resolve the image used by one SWE-bench smoke generation run."""

from __future__ import annotations


def instance_image(instance: dict, namespace: str, arch: str) -> str:
    """Use dataset image metadata or the published legacy image name."""
    image = instance.get("image")
    if isinstance(image, str) and image.strip():
        return image.strip()
    return f"{namespace}/sweb.eval.{arch}.{instance['instance_id'].lower()}:latest".replace("__", "_1776_")
