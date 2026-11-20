"""Typed local paths shared by attachment capture, export, and native restore."""

import re
from urllib.parse import unquote, urlsplit


ATTACHMENT_KINDS = {
    "image", "input_image", "image_url", "localimage", "document",
    "file", "input_file", "imagefile",
}
ATTACHMENT_PATH_FIELDS = {
    "path", "file_path", "filePath", "local_path", "localPath", "image_path", "imagePath", "url",
}


def local_file_uri_path(value):
    """Decode a local file URI without resolving it or accepting network hosts."""
    if not value.lower().startswith("file:"):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.netloc.casefold() not in {"", "localhost"} or url.query or url.fragment:
        return None
    path = unquote(url.path)
    if not path.startswith("/") or path.startswith("//") or any(ord(c) < 32 for c in path):
        return None
    return path[1:] if re.match(r"^/[a-zA-Z]:[/\\]", path) else path
