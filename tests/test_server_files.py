"""项目工作区文件端点（列表 / 读 / 写 / 新建 / 原始字节）的协议级测试。

设计与取舍见 plans/enhancement/04-workspace-files.md。这里验的是**服务端接线与护栏**：
路径逃逸（含软链接）、忽略目录、二进制/有损/截断的保护、版本冲突、换行符保留、审计。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from routivus.server import ProjectRegistry, create_app
from routivus.server.config import ServerConfig
from routivus.server.files import MAX_PREVIEW_BYTES
from routivus.server.storage import WorkspaceStore

# 1x1 透明 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _client(tmp_path: Path) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = ProjectRegistry(tmp_path / "projects.json", [workspace])
    config = ServerConfig(
        projects_file=tmp_path / "projects.json",
        workspace_roots=(workspace,),
        user_dir=tmp_path / "userdata",
        database_path=tmp_path / "workspace.sqlite3",
    )
    return TestClient(
        create_app(
            registry=registry,
            store=WorkspaceStore(tmp_path / "workspace.sqlite3"),
            config=config,
            agent_factory=lambda project, session: object(),
        )
    )


def _project(client: TestClient, name: str = "Alpha") -> dict:
    root = Path(client.app.state.project_registry.allowed_root_strings()[0]) / name.lower()
    root.mkdir(exist_ok=True)
    response = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201
    return response.json()


def _root(project: dict) -> Path:
    return Path(project["root_path"])


def _seed(project: dict, rel: str, data: bytes) -> Path:
    target = _root(project) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _list(client: TestClient, project: dict, path: str = "", **params: object):
    return client.get(
        f"/api/projects/{project['id']}/files", params={"path": path, **params}
    )


def _read(client: TestClient, project: dict, path: str):
    return client.get(f"/api/projects/{project['id']}/file", params={"path": path})


def _put(client: TestClient, project: dict, **body: object):
    return client.put(f"/api/projects/{project['id']}/file", json=body)


def _create(client: TestClient, project: dict, **body: object):
    return client.post(f"/api/projects/{project['id']}/files", json=body)


def _raw(client: TestClient, project: dict, path: str):
    return client.get(f"/api/projects/{project['id']}/file/raw", params={"path": path})


def _code(response) -> str:  # noqa: ANN001 - 测试内联助手
    """错误响应体的错误码（`{"error": {"code": ...}}`）。"""
    return str(response.json()["error"]["code"])


def _symlink_or_skip(link: Path, target: Path) -> None:
    """创建指向根外的链接；Windows 无开发者模式时退回 junction（不需要特权）。"""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt" and target.is_dir():
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
    pytest.skip("当前平台不支持创建软链接或 junction")


# ==========================================================================
# 1. 列目录
# ==========================================================================


def test_list_orders_dirs_first_and_hides_ignored(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "src/main.py", b"print(1)")
    _seed(project, "b.txt", b"b")
    _seed(project, "a.txt", b"a")
    _seed(project, "node_modules/pkg/index.js", b"x")
    _seed(project, ".git/config", b"x")

    payload = _list(client, project).json()
    names = [item["name"] for item in payload["entries"]]
    assert names == ["src", "a.txt", "b.txt"]  # 目录在前、名字排序；忽略目录默认不出现
    assert payload["path"] == ""
    assert payload["truncated"] is False

    with_ignored = _list(client, project, include_ignored=True).json()
    listed = {item["name"] for item in with_ignored["entries"]}
    assert {"node_modules", ".git"} <= listed


def test_list_reports_metadata(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "src/main.py", b"print(1)\n")

    payload = _list(client, project).json()
    entry = payload["entries"][0]
    assert entry["type"] == "dir"
    assert entry["path"] == "src"
    assert entry["mtime"]

    # 目录的 size 平台相关（Windows 为 0），文件才断言大小
    file_entry = _list(client, project, path="src").json()["entries"][0]
    assert file_entry["type"] == "file"
    assert file_entry["size"] == len(b"print(1)\n")

    sub = _list(client, project, path="src").json()
    assert sub["path"] == "src"
    assert sub["parent"] == ""
    assert sub["entries"][0]["name"] == "main.py"


# ==========================================================================
# 2. 路径逃逸
# ==========================================================================


@pytest.mark.parametrize("raw", ["../outside.txt", "..", "src/../../outside.txt"])
def test_path_traversal_rejected(tmp_path: Path, raw: str) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "src/main.py", b"x")
    assert _list(client, project, path=raw).status_code == 422


def test_absolute_path_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    response = _list(client, project, path=str(tmp_path / "elsewhere"))
    assert response.status_code == 422
    assert _code(response) == "invalid_path"


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8")
    _symlink_or_skip(_root(project) / "link", outside)

    # 列目录：软链本身在根内，可以列出但标记出来
    payload = _list(client, project).json()
    entry = next(item for item in payload["entries"] if item["name"] == "link")
    assert entry["symlink"] is True and entry["outside"] is True

    # 顺着软链往下就是越界，必须拦住
    assert _list(client, project, path="link").status_code == 422
    assert _read(client, project, "link/secret.txt").status_code == 422


# ==========================================================================
# 3. 读文件
# ==========================================================================


def test_read_text_file_reports_version_and_content(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "src/main.py", "print('hi')\n".encode("utf-8"))

    payload = _read(client, project, "src/main.py").json()
    assert payload["content"] == "print('hi')\n"
    assert payload["version"]
    assert payload["binary"] is False
    assert payload["lossy"] is False
    assert payload["truncated"] is False
    assert payload["line_ending"] == "lf"
    assert payload["editable"] is True


def test_read_detects_crlf(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "win.txt", b"a\r\nb\r\n")
    assert _read(client, project, "win.txt").json()["line_ending"] == "crlf"


def test_read_binary_has_no_content(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "blob.bin", b"\x00\x01\x02binary")
    payload = _read(client, project, "blob.bin").json()
    assert payload["binary"] is True
    assert payload["content"] == ""
    assert payload["editable"] is False


def test_read_lossy_is_not_editable(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "mixed.txt", b"ok \xff\xfe bad")
    payload = _read(client, project, "mixed.txt").json()
    assert payload["binary"] is False
    assert payload["lossy"] is True
    assert payload["editable"] is False
    assert payload["reason"]


def test_read_truncates_large_file(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "big.log", b"a" * (MAX_PREVIEW_BYTES + 1024))
    payload = _read(client, project, "big.log").json()
    assert payload["truncated"] is True
    assert payload["editable"] is False
    assert len(payload["content"].encode("utf-8")) <= MAX_PREVIEW_BYTES


def test_read_unknown_file_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    assert _read(client, project, "nope.txt").status_code == 404


# ==========================================================================
# 4. 写文件
# ==========================================================================


def test_write_creates_then_updates(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    (_root(project) / "src").mkdir()

    created = _put(client, project, path="src/new.py", content="x = 1\n")
    assert created.status_code == 200
    assert created.json()["created"] is True
    assert (_root(project) / "src/new.py").read_text(encoding="utf-8") == "x = 1\n"

    current = _read(client, project, "src/new.py").json()
    updated = _put(
        client,
        project,
        path="src/new.py",
        content="x = 2\n",
        expected_version=current["version"],
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["created"] is False
    assert body["version"] != current["version"]
    assert (_root(project) / "src/new.py").read_text(encoding="utf-8") == "x = 2\n"


def test_write_preserves_crlf(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "win.txt", b"a\r\nb\r\n")
    current = _read(client, project, "win.txt").json()

    # 浏览器 textarea 会把 CRLF 规范化成 LF，服务端要还原回去
    response = _put(
        client,
        project,
        path="win.txt",
        content="a\nb\nc\n",
        expected_version=current["version"],
    )
    assert response.status_code == 200
    assert response.json()["line_ending"] == "crlf"
    assert (_root(project) / "win.txt").read_bytes() == b"a\r\nb\r\nc\r\n"


# ==========================================================================
# 5. 冲突
# ==========================================================================


def test_write_conflicts_when_file_changed_externally(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "cfg.txt", b"v1\n")
    loaded = _read(client, project, "cfg.txt").json()

    _seed(project, "cfg.txt", b"v2\n")  # 外部改动

    response = _put(
        client,
        project,
        path="cfg.txt",
        content="mine\n",
        expected_version=loaded["version"],
    )
    assert response.status_code == 409
    assert _code(response) == "file_conflict"
    assert (_root(project) / "cfg.txt").read_text(encoding="utf-8") == "v2\n"  # 未被覆盖


def test_write_requires_version_for_existing_file(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "cfg.txt", b"v1\n")
    assert _put(client, project, path="cfg.txt", content="mine\n").status_code == 409


def test_write_force_overrides_conflict(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "cfg.txt", b"v1\n")
    _seed(project, "cfg.txt", b"v2\n")
    response = _put(client, project, path="cfg.txt", content="mine\n", force=True)
    assert response.status_code == 200
    assert response.json()["forced"] is True
    assert (_root(project) / "cfg.txt").read_text(encoding="utf-8") == "mine\n"


# ==========================================================================
# 6. 写入护栏
# ==========================================================================


def test_write_rejects_ignored_dirs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, ".git/config", b"x")
    response = _put(client, project, path=".git/config", content="y", force=True)
    assert response.status_code == 422
    assert _code(response) == "path_ignored"


def test_write_rejects_binary_and_lossy(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "blob.bin", b"\x00\x01")
    _seed(project, "mixed.txt", b"ok \xff\xfe bad")

    binary = _put(client, project, path="blob.bin", content="text", force=True)
    assert binary.status_code == 422
    assert _code(binary) == "binary_not_editable"

    lossy = _put(client, project, path="mixed.txt", content="text", force=True)
    assert lossy.status_code == 422
    assert _code(lossy) == "lossy_not_editable"


def test_write_requires_existing_parent(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    response = _put(client, project, path="no/such/dir/file.txt", content="x")
    assert response.status_code == 422
    assert _code(response) == "parent_missing"


def test_write_rejects_oversized_target(tmp_path: Path) -> None:
    """超过可编辑上限的文件不允许保存（读接口也已标 truncated）。"""
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "big.log", b"a" * (MAX_PREVIEW_BYTES + 1024))
    response = _put(client, project, path="big.log", content="short", force=True)
    assert response.status_code == 413


# ==========================================================================
# 7. 新建
# ==========================================================================


def test_create_file_and_dir(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)

    file_response = _create(client, project, path="note.md", kind="file")
    assert file_response.status_code == 201
    assert (_root(project) / "note.md").read_text(encoding="utf-8") == ""

    dir_response = _create(client, project, path="docs", kind="dir")
    assert dir_response.status_code == 201
    assert (_root(project) / "docs").is_dir()


def test_create_rejects_conflicts_and_missing_parent(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "note.md", b"x")

    duplicate = _create(client, project, path="note.md")
    assert duplicate.status_code == 409
    assert _code(duplicate) == "already_exists"

    missing = _create(client, project, path="no/dir/note.md")
    assert missing.status_code == 422


# ==========================================================================
# 8. 审计
# ==========================================================================


def test_write_is_audited(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "cfg.txt", b"v1\n")
    _put(client, project, path="cfg.txt", content="v2\n", force=True)

    log = _root(project) / ".routivus" / "audit.log"
    assert log.is_file()
    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    writes = [item for item in entries if item.get("action") == "file_write"]
    assert writes and writes[-1]["path"] == "cfg.txt"
    assert writes[-1]["forced"] is True


# ==========================================================================
# 9. 图片原始字节
# ==========================================================================


def test_raw_serves_real_image(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "pic.png", PNG_BYTES)
    response = _raw(client, project, "pic.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == PNG_BYTES


def test_raw_rejects_non_image(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "note.txt", b"hello")
    _seed(project, "fake.png", b"definitely not a png")

    assert _raw(client, project, "note.txt").status_code == 422  # 后缀不在白名单
    assert _raw(client, project, "fake.png").status_code == 422  # magic bytes 不符


# ==========================================================================
# 10. 回归与边界
# ==========================================================================


def test_unknown_project_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/projects/missing/files").status_code == 404


def test_listing_a_file_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "note.md", b"x")
    response = _list(client, project, path="note.md")
    assert response.status_code == 422
    assert _code(response) == "not_a_directory"


def test_existing_project_endpoints_still_work(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    assert client.get(f"/api/projects/{project['id']}").status_code == 200
    response = client.post(f"/api/projects/{project['id']}/sessions", json={"title": "t"})
    assert response.status_code == 201


# ==========================================================================
# 11. 删除（不可逆，护栏比写入更严）
# ==========================================================================


def _delete(client: TestClient, project: dict, path: str, **params: object):
    return client.delete(
        f"/api/projects/{project['id']}/file", params={"path": path, **params}
    )


def test_delete_file_removes_it_and_writes_audit(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    target = _seed(project, "src/old.py", b"print(1)")

    response = _delete(client, project, "src/old.py")

    assert response.status_code == 200
    assert response.json() == {
        "path": "src/old.py",
        "type": "file",
        "deleted": True,
        "recursive": False,
    }
    assert not target.exists()
    # 父目录要留着：删文件不该顺手清目录
    assert target.parent.is_dir()
    audit = _root(project) / ".routivus" / "audit.log"
    assert "file_delete" in audit.read_text(encoding="utf-8")


def test_delete_empty_dir_needs_no_recursive_flag(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    ( _root(project) / "empty" ).mkdir()

    response = _delete(client, project, "empty")

    assert response.status_code == 200
    assert not (_root(project) / "empty").exists()


def test_delete_non_empty_dir_requires_recursive_flag(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "pkg/deep/mod.py", b"x")

    blocked = _delete(client, project, "pkg")
    assert blocked.status_code == 409
    assert _code(blocked) == "directory_not_empty"
    assert (_root(project) / "pkg" / "deep" / "mod.py").exists()

    forced = _delete(client, project, "pkg", recursive="true")
    assert forced.status_code == 200
    assert forced.json()["type"] == "dir"
    assert forced.json()["recursive"] is True
    assert not (_root(project) / "pkg").exists()


def test_delete_rejects_root_ignored_protected_and_escape(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project = _project(client)
    _seed(project, "node_modules/pkg/index.js", b"x")
    _seed(project, ".routivus/audit.log", b"{}\n")
    _seed(project, "keep.txt", b"x")

    root = _delete(client, project, "")
    assert (root.status_code, _code(root)) == (422, "invalid_path")

    ignored = _delete(client, project, "node_modules/pkg/index.js")
    assert (ignored.status_code, _code(ignored)) == (422, "path_ignored")

    protected = _delete(client, project, ".routivus/audit.log")
    assert (protected.status_code, _code(protected)) == (422, "path_protected")

    escape = _delete(client, project, "../outside.txt")
    assert (escape.status_code, _code(escape)) == (422, "invalid_path")

    missing = _delete(client, project, "nope.txt")
    assert (missing.status_code, _code(missing)) == (404, "path_not_found")

    assert (_root(project) / "keep.txt").exists()
    assert (_root(project) / ".routivus" / "audit.log").exists()


def test_delete_symlink_removes_link_only(tmp_path: Path) -> None:
    """软链接只摘链接本身：按解析后的路径删会删掉目标、留下悬空链接。

    目标故意用**目录**：Windows 无开发者模式时 `symlink_to` 起不来，而指向目录时
    助手会退回 junction（不需要特权），这条用例才真的跑得起来。
    """
    client = _client(tmp_path)
    project = _project(client)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("真实内容", encoding="utf-8")
    link = _root(project) / "linked"
    _symlink_or_skip(link, outside)

    response = _delete(client, project, "linked")

    assert response.status_code == 200
    assert response.json()["type"] == "link"
    assert not link.is_symlink() and not link.exists()
    # 目标目录与里面的文件必须原样还在
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "真实内容"


def test_delete_unknown_project_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.delete("/api/projects/missing/file", params={"path": "a.txt"}).status_code == 404
