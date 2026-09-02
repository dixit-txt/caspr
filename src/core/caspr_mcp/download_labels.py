"""Friendly download labels for MCP export links (no DB changes)."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse


def filename_from_s3_uri(s3_uri: str | None) -> str | None:
    """Extract the trailing filename from an S3 URI returned by generate-report."""
    if not s3_uri:
        return None
    path = s3_uri.split("?")[0].rstrip("/")
    name = unquote(path.rsplit("/", 1)[-1])
    return name or None


def prettify_download_label(filename: str) -> str:
    """Turn ``ASEAN_Energy_Transition.pdf`` into ``ASEAN Energy Transition.pdf``."""
    if "." not in filename:
        return filename.replace("_", " ")
    stem, ext = filename.rsplit(".", 1)
    return f"{stem.replace('_', ' ')}.{ext}"


def filename_from_presigned_url(url: str | None) -> str | None:
    """Extract filename from S3 presigned URL Content-Disposition when present."""
    if not url:
        return None
    disp_values = parse_qs(urlparse(url).query).get("response-content-disposition")
    if not disp_values:
        return None
    disp = disp_values[0]
    if "filename*=" in disp:
        encoded = disp.split("filename*=", 1)[-1]
        if "''" in encoded:
            encoded = encoded.split("''", 1)[-1]
        return unquote(encoded)
    if "filename=" in disp:
        raw = disp.split("filename=", 1)[-1].strip().strip('"')
        return unquote(raw)
    return None


def resolve_download_label(
    s3_uri: str | None,
    *,
    file_type: str,
    version: int | None = None,
) -> str:
    """Best-effort human-readable label for a report export."""
    raw = filename_from_s3_uri(s3_uri)
    if raw:
        label = prettify_download_label(raw)
    else:
        label = f"report.{file_type.lstrip('.')}"

    if version is not None and f"v{version}" not in label.lower():
        if "." in label:
            stem, ext = label.rsplit(".", 1)
            label = f"{stem} v{version}.{ext}"
        else:
            label = f"{label} v{version}"

    return label


def resolve_download_label_for_url(
    url: str,
    *,
    s3_uri: str | None = None,
    file_type: str,
    version: int | None = None,
) -> str:
    """Prefer Content-Disposition filename; fall back to s3_uri or generic label."""
    label = filename_from_presigned_url(url)
    if label:
        return label
    return resolve_download_label(s3_uri, file_type=file_type, version=version)


def build_download_markdown(label: str, url: str) -> str:
    """Markdown link: short visible label, real presigned URL behind it."""
    return f"[{label}]({url})"
