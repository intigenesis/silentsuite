"""A scriptable stand-in for the GitHub REST endpoints the release lanes use.

The admission helper, the attachment helper and the readiness gate are all shell
or Python that talks to api.github.com. Reading them proves very little; running
them against a repository whose tag has just moved, whose draft is published, or
whose release list is 400 entries long proves quite a lot. This serves exactly
the endpoints those three touch, in the shapes GitHub returns them, so the tests
can put the repository into states that are impossible to arrange for real.

It is deliberately literal about the awkward parts: asset downloads answer 302
to a separate unauthenticated URL, release listing is paginated at 100, and
`target_commitish` is echoed back from creation the way GitHub does for drafts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

REPOSITORY = "silent-suite/silentsuite"
DEFAULT_BRANCH = "main"

CREATION_RULESET = {
    "id": 20051354,
    "name": "Authorize v* release tag creation",
    "target": "tag",
    "source_type": "Repository",
    "source": REPOSITORY,
    "enforcement": "active",
    "conditions": {"ref_name": {"exclude": [], "include": ["refs/tags/v*"]}},
    "rules": [{"type": "creation"}],
}
IMMUTABILITY_RULESET = {
    "id": 20051355,
    "name": "Make v* release tags immutable",
    "target": "tag",
    "source_type": "Repository",
    "source": REPOSITORY,
    "enforcement": "active",
    "conditions": {"ref_name": {"exclude": [], "include": ["refs/tags/v*"]}},
    "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
}


def default_state(tag: str = "v1.2.3", commit: str = "a" * 40) -> dict:
    """A repository where exactly one immutable tag is ready to be released."""

    return {
        "repository": REPOSITORY,
        "default_branch": DEFAULT_BRANCH,
        "tags": {tag: {"type": "commit", "sha": commit}},
        "annotated": {},
        # sha -> (compare status, merge base). Anything absent is "diverged".
        "compare": {commit: ("behind", commit)},
        # Deep copies: a test that relaxes one ruleset must not leak that
        # relaxation into the next test through the module-level template.
        "rulesets": [copy.deepcopy(CREATION_RULESET), copy.deepcopy(IMMUTABILITY_RULESET)],
        "releases": [],
        "assets": {},
        "asset_bytes": {},
        "next_release_id": 100,
        "next_asset_id": 900,
        "requests": [],
        "fail": {},
        # Move the tag out from under a lane mid-run: after this many reads of
        # the tag ref, serve `moved_sha` instead. Models the window between a
        # pre-mutation check and the post-mutation one.
        "tag_moves_after": None,
        "moved_sha": "f" * 40,
        "tag_reads": 0,
        # Every Content-Type the release-creation endpoint refused, so a test
        # can assert *why* a request failed rather than only that it did.
        "rejected_media_types": [],
    }


class _Handler(BaseHTTPRequestHandler):
    state: dict

    def log_message(self, *args):  # noqa: D102 - keep the test output readable
        return

    # ── plumbing ──────────────────────────────────────────────────────

    def _send(self, status: int, payload=None, headers: dict | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _override(self, key: str) -> int | None:
        return self.state["fail"].get(key)

    # ── routing ───────────────────────────────────────────────────────

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        self.state["requests"].append(("GET", path))
        repository = self.state["repository"]
        prefix = f"/repos/{repository}"

        if path == prefix:
            status = self._override("repository")
            if status:
                return self._send(status, {"message": "forced"})
            return self._send(200, {"default_branch": self.state["default_branch"]})

        match = re.fullmatch(rf"{re.escape(prefix)}/git/ref/tags/(.+)", path)
        if match:
            target = self.state["tags"].get(match.group(1))
            if target is None:
                return self._send(404, {"message": "Not Found"})
            self.state["tag_reads"] += 1
            moves_after = self.state.get("tag_moves_after")
            if moves_after is not None and self.state["tag_reads"] > moves_after:
                target = {"type": "commit", "sha": self.state["moved_sha"]}
            return self._send(
                200,
                {
                    "ref": f"refs/tags/{match.group(1)}",
                    "object": {"type": target["type"], "sha": target["sha"]},
                },
            )

        match = re.fullmatch(rf"{re.escape(prefix)}/git/tags/([0-9a-f]+)", path)
        if match:
            target = self.state["annotated"].get(match.group(1))
            if target is None:
                return self._send(404, {"message": "Not Found"})
            return self._send(200, {"object": target})

        match = re.fullmatch(rf"{re.escape(prefix)}/compare/([^.]+)\.\.\.(.+)", path)
        if match:
            status, merge_base = self.state["compare"].get(
                match.group(2), ("diverged", "0" * 40)
            )
            return self._send(
                200, {"status": status, "merge_base_commit": {"sha": merge_base}}
            )

        if path == f"{prefix}/rulesets":
            status = self._override("rulesets")
            if status:
                return self._send(status, {"message": "forced"})
            # A token narrower than administration:read is refused outright by
            # GitHub rather than downgraded, so the caller has to retry without
            # it. Model that with one reserved token value.
            if self.headers.get("Authorization") == "Bearer unprivileged":
                return self._send(403, {"message": "Resource not accessible"})
            return self._send(
                200,
                [
                    {"id": ruleset["id"], "name": ruleset["name"], "target": ruleset["target"]}
                    for ruleset in self.state["rulesets"]
                ],
            )

        match = re.fullmatch(rf"{re.escape(prefix)}/rulesets/(\d+)", path)
        if match:
            for ruleset in self.state["rulesets"]:
                if ruleset["id"] == int(match.group(1)):
                    return self._send(200, ruleset)
            return self._send(404, {"message": "Not Found"})

        if path == f"{prefix}/releases":
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            start = (page - 1) * per_page
            return self._send(200, self.state["releases"][start : start + per_page])

        match = re.fullmatch(rf"{re.escape(prefix)}/releases/(\d+)", path)
        if match:
            for release in self.state["releases"]:
                if release["id"] == int(match.group(1)):
                    return self._send(200, release)
            return self._send(404, {"message": "Not Found"})

        match = re.fullmatch(rf"{re.escape(prefix)}/releases/(\d+)/assets", path)
        if match:
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            assets = self.state["assets"].get(int(match.group(1)), [])
            start = (page - 1) * per_page
            return self._send(200, assets[start : start + per_page])

        match = re.fullmatch(rf"{re.escape(prefix)}/releases/assets/(\d+)", path)
        if match:
            asset_id = int(match.group(1))
            if asset_id not in self.state["asset_bytes"]:
                return self._send(404, {"message": "Not Found"})
            # GitHub answers 302 to a signed URL that must be fetched without
            # the API Authorization header.
            return self._send(302, None, {"Location": f"{self._base()}/raw/{asset_id}"})

        match = re.fullmatch(r"/raw/(\d+)", path)
        if match:
            payload = self.state["asset_bytes"].get(int(match.group(1)))
            if payload is None:
                return self._send(404, {"message": "Not Found"})
            return self._raw(payload)

        return self._send(404, {"message": f"unrouted {path}"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.state["requests"].append(("POST", path))
        repository = self.state["repository"]
        prefix = f"/repos/{repository}"

        if path == f"{prefix}/releases":
            status = self._override("create_release")
            if status:
                return self._send(status, self.state.get("create_release_body", {"message": "forced"}))
            # GitHub parses a request body by its declared media type. curl's
            # `-d` defaults to application/x-www-form-urlencoded, so a JSON
            # document sent without this header arrives as one nonsense form
            # field and the release is rejected — the exact defect that left
            # v0.5.4-beta with no draft. Modelled faithfully so the regression
            # cannot come back unnoticed.
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";")[0].strip().lower() != "application/json":
                self.state["rejected_media_types"].append(content_type)
                return self._send(
                    400,
                    {
                        "message": "Body should be a JSON object",
                        "documentation_url": "https://docs.github.com/rest",
                    },
                )
            try:
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"message": "Problems parsing JSON"})
            # A tag may hold exactly one release; a losing sibling in the
            # create/create race gets GitHub's 422, not a second draft.
            if any(r["tag_name"] == payload.get("tag_name") for r in self.state["releases"]):
                return self._send(
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [{"resource": "Release", "code": "already_exists"}],
                    },
                )
            release = {
                "id": self.state["next_release_id"],
                "tag_name": payload["tag_name"],
                "name": payload.get("name"),
                "draft": bool(payload.get("draft")),
                "target_commitish": payload.get("target_commitish", self.state["default_branch"]),
            }
            self.state["next_release_id"] += 1
            self.state["releases"].append(release)
            self.state["assets"][release["id"]] = []
            return self._send(201, release)

        match = re.fullmatch(rf"{re.escape(prefix)}/releases/(\d+)/assets", path)
        if match:
            release_id = int(match.group(1))
            name = query.get("name", [""])[0]
            asset_id = self.state["next_asset_id"]
            self.state["next_asset_id"] += 1
            self.state["asset_bytes"][asset_id] = body
            self.state["assets"].setdefault(release_id, []).append(
                {
                    "id": asset_id,
                    "name": name,
                    "size": len(body),
                    "state": "uploaded",
                    "digest": hashlib.sha256(body).hexdigest(),
                }
            )
            return self._send(201, {"id": asset_id, "name": name})

        return self._send(404, {"message": f"unrouted {path}"})

    def _base(self) -> str:
        return f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"


class GitHubStub:
    def __init__(self, state: dict) -> None:
        self.state = state
        handler = type("BoundHandler", (_Handler,), {"state": state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def environment(self) -> dict[str, str]:
        return {
            "GITHUB_API_URL": self.url,
            "GITHUB_UPLOAD_URL_BASE": self.url,
            "GITHUB_REPOSITORY": self.state["repository"],
        }

    def add_release(
        self,
        tag: str,
        *,
        draft: bool = True,
        target: str | None = None,
        release_id: int | None = None,
    ) -> dict:
        identifier = release_id if release_id is not None else self.state["next_release_id"]
        if release_id is None:
            self.state["next_release_id"] += 1
        release = {
            "id": identifier,
            "tag_name": tag,
            "name": f"SilentSuite {tag}",
            "draft": draft,
            "target_commitish": target if target is not None else DEFAULT_BRANCH,
        }
        self.state["releases"].append(release)
        self.state["assets"].setdefault(identifier, [])
        return release

    def add_asset(self, release_id: int, name: str, payload: bytes) -> dict:
        asset_id = self.state["next_asset_id"]
        self.state["next_asset_id"] += 1
        self.state["asset_bytes"][asset_id] = payload
        asset = {
            "id": asset_id,
            "name": name,
            "size": len(payload),
            "state": "uploaded",
            "digest": hashlib.sha256(payload).hexdigest(),
        }
        self.state["assets"].setdefault(release_id, []).append(asset)
        return asset

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
