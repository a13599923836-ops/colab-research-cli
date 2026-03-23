#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import mimetypes
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import httpx
import typer


app = typer.Typer(help="Colab Research CLI")
auth_app = typer.Typer(help="登录与本地凭据管理")
token_app = typer.Typer(help="管理 MCP token")
access_app = typer.Typer(help="审批普通用户的 MCP access")
docs_app = typer.Typer(help="文档列表、查看、下载与上传")

app.add_typer(auth_app, name="auth")
app.add_typer(token_app, name="token")
app.add_typer(access_app, name="access")
app.add_typer(docs_app, name="docs")

DEFAULT_BASE_URL = "https://paperless.colab-research.cloud"
CONFIG_PATH = Path(
    os.getenv(
        "COLAB_CLI_CONFIG_PATH",
        str(Path.home() / ".config" / "colab-research-cli" / "config.json"),
    )
)


def normalize_api_root(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URL
    if url.endswith("/mcp/cli/v1") or url.endswith("/cli/v1"):
        return url
    if url.endswith("/mcp"):
        return f"{url}/cli/v1"
    return f"{url}/mcp/cli/v1"


def config_dir() -> Path:
    return CONFIG_PATH.parent


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(data: dict[str, Any]) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_config() -> None:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


def current_api_root(override_base_url: str | None = None) -> str:
    if override_base_url:
        return normalize_api_root(override_base_url)
    config = load_config()
    return normalize_api_root(str(config.get("base_url") or DEFAULT_BASE_URL))


def current_token() -> str:
    token = str(load_config().get("token") or "").strip()
    if not token:
        raise typer.BadParameter("当前没有登录，请先执行 colab auth login。")
    return token


def client(*, base_url: str | None = None, auth_token: str | None = None) -> httpx.Client:
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return httpx.Client(
        base_url=current_api_root(base_url),
        headers=headers,
        timeout=120,
        follow_redirects=True,
        trust_env=False,
    )


def api_request(
    method: str,
    path: str,
    *,
    base_url: str | None = None,
    auth_token: str | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    data: dict[str, Any] | list[tuple[str, str]] | None = None,
) -> Any:
    token = auth_token if auth_token is not None else current_token()
    last_error: Exception | None = None
    response: httpx.Response | None = None
    for _ in range(2):
        try:
            with client(base_url=base_url, auth_token=token) as c:
                response = c.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    files=files,
                    data=data,
                )
            break
        except httpx.HTTPError as exc:
            last_error = exc
    if response is None:
        raise typer.BadParameter(f"网络请求失败：{last_error}")
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = response.json()
        if response.status_code >= 400:
            raise typer.BadParameter(str(payload.get("error") or payload))
        return payload
    if response.status_code >= 400:
        raise typer.BadParameter(response.text.strip() or f"HTTP {response.status_code}")
    return response


def emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, str):
        typer.echo(data)
        return
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def role_label(role: str) -> str:
    return {
        "owner": "owner",
        "manager": "manager",
        "approved-user": "approved-user",
        "pending": "pending",
    }.get(role, role)


def print_user_summary(user: dict[str, Any]) -> None:
    typer.echo(f"账号: {user['username']}")
    typer.echo(f"角色: {role_label(user['role'])}")
    caps = user.get("capabilities", {})
    enabled = [name for name, allowed in caps.items() if allowed]
    typer.echo(f"能力: {', '.join(enabled) if enabled else '无'}")


def content_disposition_filename(header_value: str, fallback: str) -> str:
    match = re.search(r"filename\\*=UTF-8''([^;]+)", header_value)
    if match:
        return httpx.URL(f"https://x/{match.group(1)}").path.split("/")[-1] or fallback
    match = re.search(r'filename=\"?([^\";]+)\"?', header_value)
    if match:
        return match.group(1)
    return fallback


def expand_upload_inputs(paths: list[Path], *, recursive: bool) -> list[Path]:
    expanded: list[Path] = []
    seen: set[Path] = set()

    for path in paths:
        resolved = path.expanduser().resolve()
        candidates: list[Path]
        if resolved.is_dir():
            iterator = resolved.rglob("*") if recursive else resolved.iterdir()
            candidates = sorted(item for item in iterator if item.is_file())
        else:
            candidates = [resolved]

        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)

    return expanded


def merge_upload_tags(tags: list[str] | None, *, pending: bool) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    for raw_tag in tags or []:
        tag = raw_tag.strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(tag)

    if pending and "待分类".casefold() not in seen:
        merged.append("待分类")

    return merged


def upload_one_document(
    file: Path,
    *,
    title: str,
    document_type: str,
    correspondent: str,
    storage_path: str,
    tags: list[str],
    wait_for_result: bool,
) -> Any:
    form_data: dict[str, str] = {}
    for key, value in (
        ("title", title),
        ("document_type", document_type),
        ("correspondent", correspondent),
        ("storage_path", storage_path),
    ):
        if value:
            form_data[key] = value
    if tags:
        form_data["tags"] = ",".join(tags)

    with file.open("rb") as fh:
        return api_request(
            "POST",
            "/documents/upload",
            params={"wait": "true" if wait_for_result else "false"},
            data=form_data,
            files={"document": (file.name, fh, "application/octet-stream")},
        )


def stream_upload_one_document(
    file: Path,
    *,
    title: str,
    document_type: str,
    correspondent: str,
    storage_path: str,
    tags: list[str],
) -> Any:
    content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    init_payload = api_request(
        "POST",
        "/documents/uploads/init",
        json_body={
            "filename": file.name,
            "content_type": content_type,
            "title": title,
            "document_type": document_type,
            "correspondent": correspondent,
            "storage_path": storage_path,
            "tags": tags,
        },
    )
    upload_path = str(init_payload.get("upload_path") or "").strip()
    if not upload_path:
        raise typer.BadParameter("服务器没有返回 upload_path。")

    with file.open("rb") as fh:
        with client(auth_token=current_token()) as c:
            response = c.put(
                upload_path,
                content=fh,
                headers={"Content-Type": content_type},
            )
    if response.status_code >= 400:
        content_type_header = response.headers.get("content-type", "")
        if "application/json" in content_type_header:
            payload = response.json()
            raise typer.BadParameter(str(payload.get("error") or payload))
        raise typer.BadParameter(response.text.strip() or f"HTTP {response.status_code}")
    return response.json()


def terminal_is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def current_command_name() -> str:
    return Path(sys.argv[0]).name or "colab"


def metadata_present(
    *,
    title: str,
    document_type: str,
    correspondent: str,
    storage_path: str,
    tags: list[str] | None,
    pending: bool,
    metadata_json: Path | None,
) -> bool:
    return bool(
        title.strip()
        or document_type.strip()
        or correspondent.strip()
        or storage_path.strip()
        or (tags and any(tag.strip() for tag in tags))
        or pending
        or metadata_json is not None
    )


def parse_metadata_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        tags: list[str] = []
        for item in value:
            tags.extend(part.strip() for part in str(item).split(",") if part.strip())
        return tags
    raise typer.BadParameter("metadata_json 中的 tags 必须是字符串或字符串数组。")


def normalize_metadata_entry(raw_entry: dict[str, Any]) -> dict[str, Any]:
    source = str(raw_entry.get("file") or raw_entry.get("path") or "").strip()
    if not source:
        raise typer.BadParameter("metadata_json 中每条记录都需要 file 或 path 字段。")
    return {
        "source": source,
        "title": str(raw_entry.get("title") or "").strip(),
        "document_type": str(raw_entry.get("document_type") or "").strip(),
        "correspondent": str(raw_entry.get("correspondent") or "").strip(),
        "storage_path": str(raw_entry.get("storage_path") or "").strip(),
        "tags": parse_metadata_tags(raw_entry.get("tags")),
    }


def load_metadata_json(path: Path, files: list[Path]) -> dict[Path, dict[str, Any]]:
    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"metadata_json 不是合法 JSON：{exc}") from exc

    if isinstance(raw_data, dict):
        entries_raw = raw_data.get("files")
    else:
        entries_raw = raw_data

    if not isinstance(entries_raw, list):
        raise typer.BadParameter("metadata_json 必须是数组，或形如 {\"files\": [...]} 的对象。")

    normalized_files = {str(file): file for file in files}
    normalized_resolved = {str(file.resolve()): file for file in files}
    by_name: dict[str, list[Path]] = {}
    for file in files:
        by_name.setdefault(file.name.casefold(), []).append(file)

    matched: dict[Path, dict[str, Any]] = {}
    for raw_entry in entries_raw:
        if not isinstance(raw_entry, dict):
            raise typer.BadParameter("metadata_json 中每条记录都必须是对象。")

        entry = normalize_metadata_entry(raw_entry)
        source = entry["source"]
        match: Path | None = normalized_files.get(source)

        if match is None:
            resolved_source = str(Path(source).expanduser().resolve())
            match = normalized_resolved.get(resolved_source)

        if match is None:
            basename_matches = by_name.get(Path(source).name.casefold(), [])
            if len(basename_matches) == 1:
                match = basename_matches[0]
            elif len(basename_matches) > 1:
                raise typer.BadParameter(
                    f"metadata_json 里的文件名 {source} 匹配到多个上传文件，请改用完整路径。"
                )

        if match is None:
            raise typer.BadParameter(f"metadata_json 里的文件 {source} 不在本次上传列表中。")
        if match in matched:
            raise typer.BadParameter(f"metadata_json 为同一个文件提供了重复记录：{source}")

        matched[match] = entry

    missing = [str(file) for file in files if file not in matched]
    if missing:
        raise typer.BadParameter(
            "metadata_json 缺少这些文件的分类结果：\n- " + "\n- ".join(missing)
        )

    return matched


def llm_classification_prompt(files: list[Path]) -> str:
    file_list = "\n".join(f"- {file}" for file in files)
    return (
        "请帮我为下面这些文档生成 Paperless 上传元数据。\n"
        "你需要阅读这些文件本身，而不是只看文件名。\n"
        "请只返回 JSON，不要解释。\n\n"
        "要求：\n"
        "- 每个文件输出一条记录\n"
        "- 使用我给出的 file 路径原样回填\n"
        "- 不确定的字段保留空字符串或空数组\n"
        "- tags 尽量控制在 1 到 5 个\n"
        '- storage_path 如果没有更好判断，默认填写 "投研资料库"\n'
        '- document_type 尽量从这些类型里选：["公司纪要", "行业专家", "会议论坛", "观点框架"]\n\n'
        "返回格式：\n"
        "{\n"
        '  "files": [\n'
        "    {\n"
        '      "file": "/absolute/path/to/file.pdf",\n'
        '      "title": "",\n'
        '      "document_type": "",\n'
        '      "correspondent": "",\n'
        '      "storage_path": "投研资料库",\n'
        '      "tags": []\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "待分类文件：\n"
        f"{file_list}\n"
    )


def maybe_prompt_upload_mode(
    files: list[Path],
    *,
    title: str,
    document_type: str,
    correspondent: str,
    storage_path: str,
    tags: list[str] | None,
    pending: bool,
    metadata_json: Path | None,
    as_json: bool,
) -> str:
    if as_json:
        return "upload"
    if metadata_present(
        title=title,
        document_type=document_type,
        correspondent=correspondent,
        storage_path=storage_path,
        tags=tags,
        pending=pending,
        metadata_json=metadata_json,
    ):
        return "upload"
    if not terminal_is_interactive():
        return "upload"

    typer.echo("当前这次上传没有提供元数据。")
    typer.echo("1. 空元数据上传")
    typer.echo("2. 分类上传（先让对方 LLM 生成 metadata JSON）")
    choice = typer.prompt("请选择 1 或 2", default="1").strip()
    return "classify" if choice == "2" else "upload"


@auth_app.command("login")
def auth_login(
    username: str = typer.Option(..., "--username", "-u"),
    password: str = typer.Option("", "--password", "-p", hide_input=True),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
) -> None:
    secret = password or typer.prompt("Paperless password", hide_input=True)
    with client(base_url=base_url, auth_token=None) as c:
        response = c.post("/login", json={"username": username, "password": secret})
    payload = response.json()
    if response.status_code >= 400 or not payload.get("ok"):
        raise typer.BadParameter(str(payload.get("error") or "登录失败"))

    save_config(
        {
            "base_url": base_url.rstrip("/"),
            "token": payload["token"],
            "username": payload["user"]["username"],
        }
    )
    typer.echo("CLI 登录成功。")
    print_user_summary(payload["user"])


@auth_app.command("logout")
def auth_logout() -> None:
    clear_config()
    typer.echo("本地 CLI 凭据已删除。")


@app.command("me")
def me(as_json: bool = typer.Option(False, "--json")) -> None:
    payload = api_request("GET", "/me")
    if as_json:
        emit(payload, as_json=True)
        return
    print_user_summary(payload["user"])


@token_app.command("list")
def token_list(as_json: bool = typer.Option(False, "--json")) -> None:
    payload = api_request("GET", "/tokens")
    if as_json:
        emit(payload["tokens"], as_json=True)
        return
    if not payload["tokens"]:
        typer.echo("没有有效 token。")
        return
    for token in payload["tokens"]:
        typer.echo(
            f"{token['id']}  {token['label']}  issued_to={token['issued_to']}  "
            f"expires={token['expires_at']}  last_used={token['last_used_at']}"
        )


@token_app.command("create")
def token_create(
    label: str = typer.Option(..., "--label"),
    issued_to: str = typer.Option("", "--issued-to"),
    note: str = typer.Option("", "--note"),
    expires_days: int = typer.Option(90, "--expires-days"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    payload = api_request(
        "POST",
        "/tokens",
        json_body={
            "label": label,
            "issued_to": issued_to,
            "note": note,
            "expires_days": expires_days,
        },
    )
    if output:
        output.write_text(payload["streamable_profile_json"], encoding="utf-8")
    if as_json:
        emit(payload, as_json=True)
        return
    typer.echo(f"Token: {payload['token']}")
    typer.echo(f"配置文件名: {payload['profile_filename']}")
    typer.echo("Streamable HTTP 配置:")
    typer.echo(payload["streamable_profile_json"])
    if output:
        typer.echo(f"已写入: {output}")


@token_app.command("revoke")
def token_revoke(token_id: str) -> None:
    payload = api_request("DELETE", f"/tokens/{token_id}")
    typer.echo(payload["message"])


@access_app.command("list")
def access_list(
    pending: bool = typer.Option(False, "--pending"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    payload = api_request(
        "GET",
        "/access",
        params={"pending": "true" if pending else "false"},
    )
    if as_json:
        emit(payload, as_json=True)
        return
    typer.echo(f"待审批账号: {payload['pending_count']}")
    for entry in payload["entries"]:
        typer.echo(f"{entry['username']}  {entry['status']}")


@access_app.command("grant")
def access_grant(
    username: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    payload = api_request("POST", "/access/grant", json_body={"username": username})
    if as_json:
        emit(payload, as_json=True)
        return
    typer.echo(payload["message"])


@access_app.command("revoke")
def access_revoke(
    username: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    payload = api_request("POST", "/access/revoke", json_body={"username": username})
    if as_json:
        emit(payload, as_json=True)
        return
    typer.echo(payload["message"])


@docs_app.command("list")
def docs_list(
    limit: int = typer.Option(20, "--limit"),
    pending: bool = typer.Option(False, "--pending"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    payload = api_request(
        "GET",
        "/documents",
        params={"limit": str(limit), "pending": "true" if pending else "false"},
    )
    if as_json:
        emit(payload["documents"], as_json=True)
        return
    for doc in payload["documents"]:
        typer.echo(
            f"{doc['id']}  {doc['title']}  "
            f"type={doc.get('document_type') or '-'}  "
            f"tags={','.join(doc.get('tags') or []) or '-'}"
        )


@docs_app.command("show")
def docs_show(doc_id: int, as_json: bool = typer.Option(False, "--json")) -> None:
    payload = api_request("GET", f"/documents/{doc_id}")
    if as_json:
        emit(payload["document"], as_json=True)
        return
    doc = payload["document"]
    typer.echo(f"ID: {doc['id']}")
    typer.echo(f"标题: {doc['title']}")
    typer.echo(f"类型: {doc.get('document_type') or '-'}")
    typer.echo(f"联系人: {doc.get('correspondent') or '-'}")
    typer.echo(f"保存路径: {doc.get('storage_path') or '-'}")
    typer.echo(f"标签: {', '.join(doc.get('tags') or []) or '-'}")
    typer.echo(f"原文件: {doc.get('original_file_name') or '-'}")
    typer.echo("")
    typer.echo(doc.get("content") or "")


@docs_app.command("download")
def docs_download(
    doc_id: int,
    output: Path | None = typer.Option(None, "--output", "-o"),
) -> None:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            with client(auth_token=current_token()) as c:
                with c.stream("GET", f"/documents/{doc_id}/download") as response:
                    if response.status_code >= 400:
                        payload = response.json()
                        raise typer.BadParameter(str(payload.get("error") or payload))
                    filename = output or Path(
                        content_disposition_filename(
                            response.headers.get("content-disposition", ""),
                            f"document-{doc_id}.bin",
                        )
                    )
                    with open(filename, "wb") as fh:
                        for chunk in response.iter_bytes():
                            fh.write(chunk)
            typer.echo(f"已下载到 {filename}")
            return
        except httpx.HTTPError as exc:
            last_error = exc
    raise typer.BadParameter(f"下载失败：{last_error}")


@docs_app.command("task-status")
def docs_task_status(
    task_id: str,
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    payload = api_request("GET", f"/documents/tasks/{task_id}")
    if as_json:
        emit(payload, as_json=True)
        return
    typer.echo(f"Task ID: {payload['task_id']}")
    typer.echo(f"状态: {payload['status']}")
    typer.echo(f"文档 ID: {payload.get('related_document') or '-'}")
    typer.echo(f"结果: {payload.get('result') or '-'}")


@docs_app.command("upload")
def docs_upload(
    paths: list[Path] = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=True,
        readable=True,
        resolve_path=True,
    ),
    title: str = typer.Option("", "--title"),
    document_type: str = typer.Option("", "--document-type"),
    correspondent: str = typer.Option("", "--correspondent"),
    storage_path: str = typer.Option("", "--storage-path"),
    tag: list[str] = typer.Option(None, "--tag"),
    pending: bool = typer.Option(False, "--pending", help="自动追加“待分类”标签，适合先入库后分类。"),
    no_wait: bool = typer.Option(
        False,
        "--no-wait",
        "--async",
        help="文件成功投递到服务器后立即返回 task_id，不等待 OCR/入库完成。",
    ),
    jobs: int = typer.Option(
        1,
        "--jobs",
        help="同时上传的文件数。建议从 2 到 4 开始。",
    ),
    metadata_json: Path | None = typer.Option(
        None,
        "--metadata-json",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="LLM 分类结果 JSON，按 file/path 匹配每个上传文件。",
    ),
    recursive: bool = typer.Option(False, "--recursive", help="当输入目录时，递归上传目录下全部文件。"),
    stop_on_error: bool = typer.Option(False, "--stop-on-error", help="批量上传时遇到首个失败就停止。"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    files = expand_upload_inputs(paths, recursive=recursive)
    if not files:
        raise typer.BadParameter("没有找到可上传的文件。")
    if jobs < 1:
        raise typer.BadParameter("--jobs 不能小于 1。")
    if jobs > 6:
        raise typer.BadParameter("当前建议把 --jobs 控制在 6 以内。")
    if stop_on_error and jobs > 1:
        raise typer.BadParameter("--stop-on-error 暂不支持和 --jobs > 1 同时使用。")

    if len(files) > 1 and title and metadata_json is None:
        raise typer.BadParameter("批量上传时不支持给所有文件共用同一个 --title，请改为单文件上传或移除 --title。")

    mode = maybe_prompt_upload_mode(
        files,
        title=title,
        document_type=document_type,
        correspondent=correspondent,
        storage_path=storage_path,
        tags=tag,
        pending=pending,
        metadata_json=metadata_json,
        as_json=as_json,
    )
    if mode == "classify":
        prompt_text = llm_classification_prompt(files)
        typer.echo("")
        typer.echo("把下面这段提示词发给对方 LLM，并把返回结果保存成 JSON 文件后再上传：")
        typer.echo("")
        typer.echo(prompt_text)
        typer.echo("")
        typer.echo("保存后可执行：")
        quoted_files = " ".join(shlex.quote(str(file)) for file in files)
        typer.echo(
            f"{current_command_name()} docs upload {quoted_files} --metadata-json ./metadata.json --json"
        )
        raise typer.Exit(code=0)

    metadata_by_file = load_metadata_json(metadata_json, files) if metadata_json else {}
    tags = merge_upload_tags(tag, pending=pending)
    planned_uploads: list[dict[str, Any]] = []
    for file in files:
        file_metadata = metadata_by_file.get(file, {})
        planned_uploads.append(
            {
                "file": file,
                "title": file_metadata.get("title", "") or title,
                "document_type": file_metadata.get("document_type", "") or document_type,
                "correspondent": file_metadata.get("correspondent", "") or correspondent,
                "storage_path": file_metadata.get("storage_path", "") or storage_path,
                "tags": merge_upload_tags(
                    [*tags, *file_metadata.get("tags", [])],
                    pending=False,
                ),
            }
        )

    results: list[dict[str, Any] | None] = [None] * len(planned_uploads)
    worker_count = min(jobs, len(planned_uploads))

    def run_one(index: int, upload_plan: dict[str, Any]) -> dict[str, Any]:
        file = upload_plan["file"]
        try:
            if no_wait:
                payload = stream_upload_one_document(
                    file,
                    title=upload_plan["title"],
                    document_type=upload_plan["document_type"],
                    correspondent=upload_plan["correspondent"],
                    storage_path=upload_plan["storage_path"],
                    tags=upload_plan["tags"],
                )
            else:
                payload = upload_one_document(
                    file,
                    title=upload_plan["title"],
                    document_type=upload_plan["document_type"],
                    correspondent=upload_plan["correspondent"],
                    storage_path=upload_plan["storage_path"],
                    tags=upload_plan["tags"],
                    wait_for_result=True,
                )
            return {
                "file": str(file),
                "ok": True,
                "result": payload,
            }
        except Exception as exc:
            return {
                "file": str(file),
                "ok": False,
                "error": str(exc),
            }

    if worker_count == 1:
        for index, upload_plan in enumerate(planned_uploads):
            result = run_one(index, upload_plan)
            results[index] = result
            if result["ok"]:
                if not as_json and len(files) > 1:
                    typer.echo(f"[OK] {upload_plan['file']}")
            else:
                if not as_json:
                    typer.echo(f"[FAILED] {upload_plan['file']}: {result['error']}")
                if stop_on_error:
                    break
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_index = {
                executor.submit(run_one, index, upload_plan): index
                for index, upload_plan in enumerate(planned_uploads)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                upload_plan = planned_uploads[index]
                result = future.result()
                results[index] = result
                if result["ok"]:
                    if not as_json:
                        typer.echo(f"[OK] {upload_plan['file']}")
                else:
                    if not as_json:
                        typer.echo(f"[FAILED] {upload_plan['file']}: {result['error']}")

    finalized_results = [result for result in results if result is not None]
    uploaded = sum(1 for item in finalized_results if item["ok"])
    failed = len(finalized_results) - uploaded

    if len(files) == 1:
        single = finalized_results[0]
        if not single["ok"]:
            raise typer.BadParameter(single["error"])
        if as_json:
            emit(single["result"], as_json=True)
            return
        if no_wait:
            typer.echo("上传任务已提交到服务器。")
            typer.echo(f"Task ID: {single['result'].get('task_id') or '-'}")
            typer.echo("现在可以关闭终端，服务器会继续处理。")
        else:
            typer.echo("上传成功。")
        emit(single["result"], as_json=True)
        return

    summary = {
        "ok": failed == 0,
        "waited": not no_wait,
        "jobs": worker_count,
        "uploaded_count": uploaded,
        "failed_count": failed,
        "results": finalized_results,
    }
    if as_json:
        emit(summary, as_json=True)
        return

    if no_wait:
        typer.echo(f"批量上传任务已提交：成功 {uploaded}，失败 {failed}。")
        typer.echo("现在可以关闭终端，服务器会继续处理。")
        typer.echo("后续可用 `colab docs task-status <task_id>` 查询单个任务状态。")
    else:
        typer.echo(f"批量上传完成：成功 {uploaded}，失败 {failed}。")
    if failed:
        raise typer.Exit(code=1)


@docs_app.command("update")
def docs_update(
    doc_id: int,
    title: str = typer.Option("", "--title"),
    document_type: str = typer.Option("", "--document-type"),
    correspondent: str = typer.Option("", "--correspondent"),
    storage_path: str = typer.Option("", "--storage-path"),
    tag: list[str] = typer.Option(None, "--tag"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    payload = api_request(
        "PATCH",
        f"/documents/{doc_id}",
        json_body={
            "title": title,
            "document_type": document_type,
            "correspondent": correspondent,
            "storage_path": storage_path,
            "tags": tag or [],
        },
    )
    if as_json:
        emit(payload["document"], as_json=True)
        return
    typer.echo("文档已更新。")
    emit(payload["document"], as_json=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
