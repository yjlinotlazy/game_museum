#!/usr/bin/env python3
"""Local web app for a personal retro game museum."""

from __future__ import annotations

import html
import importlib
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    import yaml
except ImportError:  # Keep the error useful instead of failing during import.
    yaml = None

CONFIG_PATH = Path.home() / ".config" / "game_museum" / "config.yaml"
PLATFORMS = ["NES", "SNES", "GB", "GBA", "N64", "PS1", "PS2", "Arcade"]
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SECTION_NAMES = ["基本资料", "简介", "随记", "存档节点"]
TRASH_DIRNAME = ".trash"
DIST_DIR = Path(__file__).resolve().parent / "dist"


def fail(message: str) -> None:
    print(f"配置错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def load_config() -> dict[str, Path]:
    if yaml is None:
        fail("需要安装 PyYAML（python3 -m pip install pyyaml）")
    if not CONFIG_PATH.is_file():
        fail(f"配置文件不存在：{CONFIG_PATH}")
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        fail(f"无法解析 YAML：{exc}")
    if not isinstance(data, dict):
        fail("配置必须是 YAML 对象")
    result = {}
    for key in ("screenshots_dir", "library_dir"):
        value = data.get(key)
        if not isinstance(value, str) or not os.path.isabs(value):
            fail(f"{key} 必须是绝对路径")
        path = Path(value).expanduser()
        if not path.is_dir():
            fail(f"{key} 目录不存在：{path}")
        result[key] = path.resolve()
    return result


def clean_trash(library: Path) -> None:
    trash = library / TRASH_DIRNAME
    trash.mkdir(exist_ok=True)
    now = datetime.now()
    marker = trash / ".last-cleanup"
    month = now.strftime("%Y-%m")
    if now.day > 7:
        return
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == month:
        return
    cutoff = datetime.now().timestamp() - 30 * 24 * 60 * 60
    for item in trash.iterdir():
        if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS:
            created = getattr(item.stat(), "st_birthtime", item.stat().st_ctime)
            if created < cutoff:
                item.unlink()
    marker.write_text(month + "\n", encoding="utf-8")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]", "_", value, flags=re.ASCII)
    return cleaned or "未命名"


def split_sections(text: str) -> tuple[str, dict[str, str], str]:
    pattern = re.compile(r"^## (基本资料|简介|随记|存档节点)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return "", {}, text
    prefix = text[: matches[0].start()]
    sections = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end() : end].strip("\n")
    suffix = text[matches[-1].end() :]
    return prefix, sections, suffix


def replace_sections(path: Path, updates: dict[str, str]) -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix, sections, suffix = split_sections(original)
    sections.update(updates)
    body = prefix.rstrip() + "\n\n" if prefix.strip() else ""
    for name in SECTION_NAMES:
        body += f"## {name}\n\n{sections.get(name, '').strip()}\n\n"
    body += suffix.lstrip("\n")
    fd, temporary = tempfile.mkstemp(prefix=".game.md.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def game_dirs(library: Path):
    for platform in PLATFORMS:
        root = library / safe_name(platform)
        if not root.is_dir():
            continue
        for directory in root.iterdir():
            if directory.is_dir() and (directory / "game.md").is_file() and not (directory / ".archived").exists():
                yield directory


def game_data(directory: Path) -> dict:
    _, sections, _ = split_sections((directory / "game.md").read_text(encoding="utf-8"))
    basic = sections.get("基本资料", "")
    platform_match = re.search(r"^- 平台：(.+)$", basic, re.MULTILINE)
    name_match = re.search(r"^- 游戏名：(.+)$", basic, re.MULTILINE)
    return {
        "path": directory,
        "name": name_match.group(1).strip() if name_match else directory.name,
        "platform": platform_match.group(1).strip() if platform_match else directory.parent.name,
        "description": sections.get("简介", "").strip(),
        "note": sections.get("随记", "").strip(),
        "sections": sections,
    }


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def render_markdown(value: str) -> str:
    """Small safe renderer for the short local notes used by the app."""
    lines = []
    for raw in value.splitlines():
        line = esc(raw)
        if line.startswith("### "):
            lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            lines.append(f"<li>{line[2:]}</li>")
        elif line:
            line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
            line = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", line)
            lines.append(f"<p>{line}</p>")
    return "".join(lines)


def parse_nodes(value: str) -> list[dict[str, str]]:
    nodes = []
    matches = list(re.finditer(r"^### (.+?)\s*$", value, re.MULTILINE))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        block = value[match.end():end].strip()
        file_match = re.search(r"^- 存档文件：(.+)$", block, re.MULTILINE)
        description = re.sub(r"^- 存档文件：.+\n?", "", block, count=1).strip() if file_match else block
        nodes.append({"name": match.group(1).strip(), "file": file_match.group(1).strip() if file_match else "", "description": description})
    return nodes


def serialize_nodes(nodes: list[dict[str, str]]) -> str:
    blocks = []
    for node in nodes:
        block = f"### {node['name']}\n\n"
        if node.get("file"):
            block += f"- 存档文件：{node['file']}\n\n"
        if node.get("description"):
            block += node["description"].strip() + "\n\n"
        blocks.append(block.rstrip())
    return "\n\n".join(blocks)


def invisible_names(directory: Path) -> set[str]:
    marker = directory / "invisible.txt"
    if not marker.is_file():
        return set()
    return {line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line.strip()}


def set_invisible(directory: Path, filename: str, invisible: bool) -> None:
    marker = directory / "invisible.txt"
    names = invisible_names(directory)
    if invisible:
        names.add(filename)
    else:
        names.discard(filename)
    if names:
        marker.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
    elif marker.exists():
        marker.unlink()


def visibility_icon(hidden: bool) -> str:
    if hidden:
        return "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M9.9 4.2A10.8 10.8 0 0112 4c5 0 8.8 4 10 8a11.8 11.8 0 01-3.1 5.2M6.2 6.2A11.8 11.8 0 002 12c1.2 4 5 8 10 8a10.8 10.8 0 003.3-.5'/></svg>"
    return "<svg viewBox='0 0 24 24' aria-hidden='true'><path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/></svg>"


def image_card(path: Path, library: Path, hidden: bool) -> str:
    relative = str(path.relative_to(library))
    eye = visibility_icon(hidden)
    return (f"<div class='shot' id='shot-{esc(path.name)}'><img class='thumb' src='/media/{quote(relative)}'>"
            f"<form class='admin-only' method='post'><input type='hidden' name='image' value='{esc(relative)}'><input type='hidden' name='action' value='toggle_visibility'><button class='eye' type='submit' name='submit_action' value='toggle_visibility' title='切换显示' aria-label='切换显示'>{eye}</button></form>"
            f"<form class='admin-only' method='post'><input type='hidden' name='image' value='{esc(relative)}'><input type='hidden' name='action' value='delete_image'><button class='trash' type='submit' name='submit_action' value='delete_image' title='移动到垃圾桶' aria-label='移动到垃圾桶' onclick=\"return confirm('移动到垃圾桶？')\">×</button></form></div>")


def page(title: str, content: str, mode: str) -> bytes:
    label = "馆长模式" if mode == "curator" else "老总模式"
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>body{{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;line-height:1.6;color:#222}}a{{color:#1464a0}}input,select,textarea,button{{font:inherit;padding:.45rem;margin:.2rem 0}}textarea{{width:100%;min-height:8rem}}textarea.description{{min-height:3rem}}.game{{padding:.7rem 0;border-bottom:1px solid #ddd}}.muted{{color:#777}}.error{{color:#a00}}.shot{{display:inline-block;position:relative;margin:.4rem}}img.thumb{{max-width:180px;max-height:140px;display:block;object-fit:contain;cursor:pointer}}.trash{{position:absolute;right:0;top:0;border:0;background:#c62828;color:white;border-radius:50%;width:1.5rem;height:1.5rem;line-height:1rem;padding:0;cursor:pointer;font-weight:bold}}#mode{{margin-bottom:1rem}}.guest .admin-only{{display:none}}#viewer{{display:none;position:fixed;inset:0;background:#000d;align-items:center;justify-content:center;z-index:9999}}#viewer.open{{display:flex}}#viewer img{{max-width:85vw;max-height:85vh}}#viewer button{{position:fixed;background:#fff;border:0;font-size:2rem;cursor:pointer;z-index:10000}}#previous{{left:2vw}}#next{{right:2vw}}#close{{top:2vh;right:2vw}}</style></head><body class='{mode}'><nav id='mode'>{label} · <a href='/toggle-mode'>切换模式</a></nav>{content}<div id='viewer'><button type='button' id='previous' aria-label='上一张'>‹</button><img id='large-image' alt='截图大图'><button type='button' id='next' aria-label='下一张'>›</button><button type='button' id='close' aria-label='关闭'>×</button></div><script>const gallery=[...document.querySelectorAll('img.thumb')].map(image=>image.src);let current=0;const viewer=document.getElementById('viewer'),large=document.getElementById('large-image');function show(index){{if(!gallery.length)return;current=(index+gallery.length)%gallery.length;large.src=gallery[current];viewer.classList.add('open')}}document.querySelectorAll('img.thumb').forEach((image,index)=>image.onclick=()=>show(index));document.getElementById('previous').onclick=()=>show(current-1);document.getElementById('next').onclick=()=>show(current+1);document.getElementById('close').onclick=()=>viewer.classList.remove('open');viewer.onclick=event=>{{if(event.target===viewer)viewer.classList.remove('open')}};</script></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    config: dict[str, Path] = {}

    def send_page(self, content: str, status=HTTPStatus.OK):
        mode = self.mode()
        if mode == "guest":
            content = "<style>.guest form:not(:has(input[name='q'])){display:block}.guest form:not(:has(input[name='q'])) textarea,.guest form:not(:has(input[name='q'])) button,.guest form:not(:has(input[name='q'])) input,.guest form:not(:has(input[name='q'])) select{display:none}</style><script>document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('h2').forEach(heading=>{const title=heading.textContent.trim();if(title.includes('基本资料')||title.includes('存档')){heading.style.display='none';let item=heading.nextElementSibling;while(item && item.tagName!=='H2'){item.style.display='none';item=item.nextElementSibling}}if((title==='简介'||title==='随记')&&!heading.nextElementSibling?.nextElementSibling?.textContent.trim()){heading.style.display='none';let item=heading.nextElementSibling;while(item && item.tagName!=='H2'){item.style.display='none';item=item.nextElementSibling}}})})</script>" + content
        content = "<style>.eye{position:absolute;right:1.7rem;top:0;border:0;background:#1565c0;color:white;border-radius:50%;width:1.5rem;height:1.5rem;line-height:1rem;padding:0;cursor:pointer;font-weight:bold}</style>" + content
        if mode == "guest":
            content = "<style>.guest .shot{width:calc(50% - 1rem);box-sizing:border-box}.guest img.thumb{width:100%;max-width:none;max-height:none;height:auto}</style>" + content
        content = "<script>document.addEventListener('submit',async event=>{const form=event.target;if(!form.closest('.shot'))return;event.preventDefault();const button=event.submitter;const data=new FormData(form);if(button&&button.name)data.set(button.name,button.value);const response=await fetch(form.action||location.href,{method:'POST',body:data});if(!response.ok){location.reload();return}if(button?.value==='delete_image'){form.closest('.shot').remove()}else if(button?.value==='toggle_visibility'){const hidden=button.innerHTML.includes('M3 3l18 18');button.innerHTML=hidden?\"<svg width='16' height='16' viewBox='0 0 24 24'><path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/></svg>\":\"<svg width='16' height='16' viewBox='0 0 24 24'><path d='M3 3l18 18M10.6 10.6a2 2 0 002.8 2.8M9.9 4.2A10.8 10.8 0 0112 4c5 0 8.8 4 10 8a11.8 11.8 0 01-3.1 5.2M6.2 6.2A11.8 11.8 0 002 12c1.2 4 5 8 10 8a10.8 10.8 0 003.3-.5'/></svg>\"}});</script>" + content
        content = "<script>document.addEventListener('submit',()=>sessionStorage.setItem('museum_scroll',String(window.scrollY)));window.addEventListener('load',()=>{const y=sessionStorage.getItem('museum_scroll');if(y!==null){sessionStorage.removeItem('museum_scroll');window.scrollTo(0,Number(y))}});</script>" + content
        data = page("复古游戏博物馆", content, mode)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def mode(self) -> str:
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"(?:^|;\s*)museum_mode=(guest|curator)", cookie)
        if match:
            return match.group(1)
        agent = self.headers.get("User-Agent", "").lower()
        return "guest" if any(token in agent for token in ("mobile", "android", "iphone", "ipad")) else "curator"

    def redirect(self, location: str):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/toggle-mode":
            target = "guest" if self.mode() == "curator" else "curator"
            back = self.headers.get("Referer", "/")
            self.send_response(HTTPStatus.SEE_OTHER); self.send_header("Location", back); self.send_header("Set-Cookie", f"museum_mode={target}; Path=/; SameSite=Lax"); self.end_headers(); return
        if parsed.path == "/api/games":
            games = sorted((game_data(p) for p in game_dirs(self.config["library_dir"])), key=lambda x: (x["name"].casefold(), x["platform"].casefold()))
            self.send_json({"games": [{"name": g["name"], "platform": g["platform"], "path": str(g["path"].relative_to(self.config["library_dir"]))} for g in games]})
            return
        if parsed.path.startswith("/api/game/"):
            relative = unquote(parsed.path[len("/api/game/"):])
            directory = (self.config["library_dir"] / relative).resolve()
            if directory not in list(game_dirs(self.config["library_dir"])):
                self.send_json({"error": "没有找到游戏"}, HTTPStatus.NOT_FOUND)
                return
            data = game_data(directory)
            images = sorted((p for p in (directory / "screenshots").glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=lambda p: p.stat().st_mtime)
            creative_images = sorted((p for p in (directory / "creative").glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=lambda p: p.stat().st_mtime)
            hidden = invisible_names(directory)
            if self.mode() == "guest":
                images = [p for p in images if p.name not in hidden]
            basic = data["sections"].get("基本资料", "")
            save_match = re.search(r"^- 存档目录：(.+)$", basic, re.MULTILINE)
            save_dir = "" if not save_match or save_match.group(1).strip() == "没有" else save_match.group(1).strip()
            save_files = [p.name for p in Path(save_dir).iterdir() if p.is_file()] if save_dir and Path(save_dir).is_dir() else []
            self.send_json({"name": data["name"], "platform": data["platform"], "save_dir": save_dir, "description": data["description"], "note": data["note"], "images": [str(p.relative_to(self.config["library_dir"])) for p in images], "creative_images": [str(p.relative_to(self.config["library_dir"])) for p in creative_images], "hidden_images": sorted(hidden), "save_files": save_files, "nodes": parse_nodes(data["sections"].get("存档节点", ""))})
            return
        if DIST_DIR.is_dir() and (parsed.path == "/" or parsed.path == "/new" or parsed.path.startswith("/game/") or parsed.path.startswith("/assets/")):
            self.frontend(parsed.path.removeprefix("/"))
            return
        if parsed.path.startswith("/media/"):
            self.media(unquote(parsed.path[7:]))
            return
        if parsed.path == "/":
            self.index(parse_qs(parsed.query).get("q", [""])[0])
        elif parsed.path.startswith("/game/"):
            self.detail(unquote(parsed.path[6:]))
        else:
            self.send_page("<h1>404</h1>", HTTPStatus.NOT_FOUND)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            parsed = urlparse(self.path)
            if parsed.path == "/api/games":
                self.create_api(json.loads(raw or "{}"))
                return
            if parsed.path.startswith("/api/game/"):
                self.update_api(unquote(parsed.path[len("/api/game/"):]), json.loads(raw or "{}"))
                return
            values = {k: v[0] for k, v in parse_qs(raw).items()}
            if parsed.path == "/games/new":
                self.create(values)
            elif parsed.path.startswith("/game/"):
                self.update(unquote(parsed.path[6:]), values)
            else:
                self.send_page("<h1>404</h1>", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            traceback.print_exc()
            if not self.wfile.closed:
                self.send_page(f"<h1>操作失败</h1><p class='error'>{esc(exc)}</p>", HTTPStatus.INTERNAL_SERVER_ERROR)

    def create_api(self, values: dict):
        name, platform = str(values.get("name", "")).strip(), values.get("platform", "")
        save_dir = str(values.get("save_dir", "")).strip()
        if not name or platform not in PLATFORMS:
            self.send_json({"error": "游戏名和平台不能为空"}, HTTPStatus.BAD_REQUEST); return
        if save_dir and not Path(save_dir).is_dir():
            self.send_json({"error": "存档目录不存在或不是目录"}, HTTPStatus.BAD_REQUEST); return
        directory = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if directory.exists():
            self.send_json({"error": "游戏已存在"}, HTTPStatus.CONFLICT); return
        directory.mkdir(parents=True); (directory / "screenshots").mkdir(); (directory / "saves").mkdir()
        replace_sections(directory / "game.md", {"基本资料": f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{save_dir or '没有'}", "简介": str(values.get("description", ""))})
        self.send_json({"path": str(directory.relative_to(self.config["library_dir"]))}, HTTPStatus.CREATED)

    def update_api(self, relative: str, values: dict):
        directory = (self.config["library_dir"] / relative).resolve()
        if directory not in list(game_dirs(self.config["library_dir"])):
            self.send_json({"error": "没有找到游戏"}, HTTPStatus.NOT_FOUND); return
        path = directory / "game.md"
        _, sections, _ = split_sections(path.read_text(encoding="utf-8"))
        basic = sections.get("基本资料", "")
        save_match = re.search(r"^- 存档目录：(.+)$", basic, re.MULTILINE)
        save_dir = "" if not save_match or save_match.group(1).strip() == "没有" else save_match.group(1).strip()
        action = values.get("action", "save")
        if action == "archive":
            (directory / ".archived").touch(); self.send_json({"ok": True}); return
        if action == "delete":
            shutil.rmtree(directory); self.send_json({"ok": True}); return
        if action == "scrape":
            duration = int(values.get("duration", 300)); now = datetime.now().timestamp(); destination = directory / "screenshots"; destination.mkdir(exist_ok=True); moved = []
            for item in self.config["screenshots_dir"].iterdir():
                if item.is_file() and item.suffix.lower() in IMAGE_EXTENSIONS and now - duration <= item.stat().st_mtime <= now:
                    target = destination / item.name; counter = 1
                    while target.exists(): target = destination / f"{item.stem}_{counter}{item.suffix}"; counter += 1
                    shutil.move(str(item), str(target)); moved.append(str(target.relative_to(self.config["library_dir"])))
            self.send_json({"ok": True, "moved": moved}); return
        if action in {"toggle_visibility", "delete_image"}:
            image = (self.config["library_dir"] / str(values.get("image", ""))).resolve()
            screenshots = (directory / "screenshots").resolve()
            if not image.is_file() or screenshots not in image.parents:
                self.send_json({"error": "图片不存在"}, HTTPStatus.NOT_FOUND); return
            if action == "toggle_visibility":
                set_invisible(directory, image.name, image.name not in invisible_names(directory))
            else:
                trash = self.config["library_dir"] / TRASH_DIRNAME; trash.mkdir(exist_ok=True)
                target = trash / image.name; counter = 1
                while target.exists():
                    target = trash / f"{image.stem}_{counter}{image.suffix}"; counter += 1
                shutil.move(str(image), str(target))
            self.send_json({"ok": True}); return
        if action in {"node_add", "node_save", "node_delete"}:
            nodes = parse_nodes(sections.get("存档节点", ""))
            index = int(values.get("index", -1))
            node_file = str(values.get("file", ""))
            if node_file and node_file not in ({p.name for p in Path(save_dir).iterdir() if p.is_file()} if save_dir and Path(save_dir).is_dir() else set()):
                self.send_json({"error": "存档文件不存在"}, HTTPStatus.BAD_REQUEST); return
            if action == "node_add":
                nodes.append({"name": str(values.get("name", "")).strip(), "file": node_file, "description": str(values.get("description", ""))})
            elif 0 <= index < len(nodes):
                if action == "node_delete": nodes.pop(index)
                else: nodes[index] = {"name": str(values.get("name", "")).strip(), "file": node_file, "description": str(values.get("description", ""))}
            replace_sections(path, {"存档节点": serialize_nodes(nodes)})
            self.send_json({"ok": True, "nodes": nodes}); return
        name, platform = str(values.get("name", "")).strip(), values.get("platform", "")
        save_dir = str(values.get("save_dir", "")).strip()
        if not name or platform not in PLATFORMS:
            self.send_json({"error": "游戏名和平台不能为空"}, HTTPStatus.BAD_REQUEST); return
        if save_dir and not Path(save_dir).is_dir():
            self.send_json({"error": "存档目录不存在或不是目录"}, HTTPStatus.BAD_REQUEST); return
        target = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if target != directory and target.exists():
            self.send_json({"error": "目标游戏已存在"}, HTTPStatus.CONFLICT); return
        basic = f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{save_dir or '没有'}"
        replace_sections(path, {"基本资料": basic, "简介": str(values.get("description", sections.get("简介", ""))), "随记": str(values.get("note", sections.get("随记", "")))})
        if target != directory:
            target.parent.mkdir(parents=True, exist_ok=True); directory.rename(target); relative = str(target.relative_to(self.config["library_dir"]))
        self.send_json({"ok": True, "path": relative})

    def index(self, query: str):
        games = sorted((game_data(p) for p in game_dirs(self.config["library_dir"])), key=lambda x: (x["name"].casefold(), x["platform"].casefold()))
        if query:
            games = [g for g in games if query.casefold() in g["name"].casefold()]
        rows = "".join(f"<div class='game'><a href='/game/{quote(str(g['path'].relative_to(self.config['library_dir'])))}'><strong>{esc(g['name'])}</strong></a> <span class='muted'>{esc(g['platform'])}</span></div>" for g in games)
        if not rows:
            rows = "<p class='muted'>没有游戏</p>" if not query else "<p class='muted'>没有找到游戏</p>"
        form = "<form><input name='q' placeholder='搜索游戏名' value='%s'><button>搜索</button></form>" % esc(query)
        new = "<p class='admin-only'><a href='/game/new'>新建游戏</a></p>"
        self.send_page(f"<h1>复古游戏博物馆</h1>{form}{new}{rows}")

    def detail(self, relative: str):
        if relative == "new":
            options = "".join(f"<option>{esc(p)}</option>" for p in PLATFORMS)
            self.send_page(f"<h1>新建游戏</h1><form class='admin-only' method='post' action='/games/new'><p>游戏名<br><input name='name' required></p><p>平台<br><select name='platform'>{options}</select></p><p>存档目录路径（可选）<br><input name='save_dir'></p><p>简介<br><textarea name='description'></textarea></p><button>保存</button> <a href='/'>取消</a></form>")
            return
        directory = (self.config["library_dir"] / relative).resolve()
        if directory not in list(game_dirs(self.config["library_dir"])):
            self.send_page("<h1>没有找到游戏</h1>", HTTPStatus.NOT_FOUND)
            return
        data = game_data(directory)
        invisible = invisible_names(directory)
        images = sorted((p for p in (directory / "screenshots").glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and (self.mode() == "curator" or p.name not in invisible)), key=lambda p: p.stat().st_mtime)
        image_html = "".join(f"<div class='shot' id='shot-{esc(p.name)}'><img class='thumb' src='/media/{quote(str(p.relative_to(self.config['library_dir'])))}'><form class='admin-only' method='post'><input type='hidden' name='image' value='{esc(str(p.relative_to(self.config['library_dir'])))}'><button class='eye' name='action' value='toggle_visibility' title='切换显示' aria-label='切换显示'>{'🙈' if p.name in invisible else '👁'}</button><button class='trash' title='移动到垃圾桶' aria-label='移动到垃圾桶' name='action' value='delete_image' onclick=\"return confirm('移动到垃圾桶？')\">×</button></form></div>" for p in images)
        image_html = image_html.replace("🙈", visibility_icon(True)).replace("👁", visibility_icon(False))
        image_html = "".join(image_card(p, self.config["library_dir"], p.name in invisible) for p in images)
        basic = data["sections"].get("基本资料", "")
        save_match = re.search(r"^- 存档目录：(.+)$", basic, re.MULTILINE)
        save_dir = "" if not save_match or save_match.group(1).strip() == "没有" else save_match.group(1).strip()
        platform_options = "".join(f"<option {'selected' if p == data['platform'] else ''}>{esc(p)}</option>" for p in PLATFORMS)
        ranges = {"300": "最近 5 分钟", "600": "最近 10 分钟", "1800": "最近 30 分钟", "3600": "最近 60 分钟", "18000": "最近 5 小时"}
        range_options = "".join(f"<option value='{seconds}'>{label}</option>" for seconds, label in ranges.items())
        nodes = parse_nodes(data['sections'].get('存档节点', ''))
        save_files = []
        if save_dir and Path(save_dir).is_dir():
            save_files = [p for p in Path(save_dir).iterdir() if p.is_file()]
        file_options = "<option value=''>没有关联存档文件</option>" + "".join(f"<option value='{esc(str(p))}'>{esc(p.name)}</option>" for p in save_files)
        node_cards = []
        for index, node in enumerate(nodes):
            options = "<option value=''>没有关联存档文件</option>" + "".join(f"<option value='{esc(str(p))}' {'selected' if str(p) == node['file'] else ''}>{esc(p.name)}</option>" for p in save_files)
            node_cards.append(f"<details><summary>{esc(node['name'])}</summary><form class='admin-only' method='post'><input type='hidden' name='node_index' value='{index}'><p>节点名称<br><input name='node_name' value='{esc(node['name'])}' required></p><p>存档文件<br><select name='node_file'>{options}</select></p><p>说明<br><textarea name='node_description'>{esc(node['description'])}</textarea></p><button name='action' value='node_save'>保存节点</button> <button name='action' value='node_delete'>删除节点</button></form></details>")
        node_html = "".join(node_cards)
        add_node = f"<form method='post'><h3>新增节点</h3><p>节点名称<br><input name='node_name' required></p><p>存档文件<br><select name='node_file'>{file_options}</select></p><p>说明<br><textarea name='node_description'></textarea></p><button name='action' value='node_add'>新增节点</button></form>"
        save_html = "".join(f"<li>{esc(p.name)} <button type='button' onclick=\"navigator.clipboard.writeText({json.dumps(str(p))})\">复制路径</button></li>" for p in save_files)
        self.send_page(f"<p><a href='/'>← 游戏列表</a></p><h1>{esc(data['name'])} <small>{esc(data['platform'])}</small></h1><form method='post'><h2>基本资料</h2><p>游戏名<br><input name='name' value='{esc(data['name'])}' required></p><p>平台<br><select name='platform'>{platform_options}</select></p><p>存档目录路径（可选）<br><input name='save_dir' value='{esc(save_dir)}'></p><h2>简介</h2><textarea class='description' name='description'>{esc(data['description'])}</textarea><div>{render_markdown(data['description'])}</div><h2>随记</h2><textarea name='note'>{esc(data['note'])}</textarea><div>{render_markdown(data['note'])}</div><p><button name='action' value='save'>保存</button></p></form><form method='post'><button name='action' value='archive'>归档</button> <button name='action' value='delete' onclick=\"return confirm('确定删除资料、截图和存档？ROM 不会被删除。')\">删除</button></form><h2>截图</h2><form method='post'><select name='duration'>{range_options}</select><button name='action' value='scrape'>搜刮截图</button></form>{image_html}<h2>存档文件</h2><ul>{save_html}</ul><h2>存档节点</h2>{node_html}{add_node}")

    def create(self, values):
        name, platform = values.get("name", "").strip(), values.get("platform", "")
        if not name or platform not in PLATFORMS:
            self.send_page("<p class='error'>游戏名和平台不能为空。</p>", HTTPStatus.BAD_REQUEST); return
        save_dir = values.get("save_dir", "").strip()
        if save_dir and not Path(save_dir).is_dir():
            self.send_page("<p class='error'>存档目录不存在或不是目录。</p>", HTTPStatus.BAD_REQUEST); return
        directory = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if directory.exists():
            self.send_page("<p class='error'>游戏已存在。</p>", HTTPStatus.CONFLICT); return
        directory.mkdir(parents=True); (directory / "screenshots").mkdir(); (directory / "saves").mkdir()
        basic = f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{save_dir or '没有'}"
        replace_sections(directory / "game.md", {"基本资料": basic, "简介": values.get("description", "")})
        self.redirect("/game/" + quote(str(directory.relative_to(self.config["library_dir"]))))

    def update(self, relative: str, values):
        directory = (self.config["library_dir"] / relative).resolve()
        if directory not in list(game_dirs(self.config["library_dir"])):
            self.send_page("<h1>没有找到游戏</h1>", HTTPStatus.NOT_FOUND); return
        action = values.get("action", "save")
        if action == "delete_image":
            image = (self.config["library_dir"] / values.get("image", "")).resolve()
            screenshots_dir = (directory / "screenshots").resolve()
            if image.is_file() and screenshots_dir in image.parents and image.suffix.lower() in IMAGE_EXTENSIONS:
                trash = self.config["library_dir"] / TRASH_DIRNAME
                trash.mkdir(exist_ok=True)
                target = trash / image.name
                counter = 1
                while target.exists():
                    target = trash / f"{image.stem}_{counter}{image.suffix}"
                    counter += 1
                shutil.move(str(image), str(target))
            self.redirect("/game/" + quote(relative))
            return
        if action == "toggle_visibility":
            image = (self.config["library_dir"] / values.get("image", "")).resolve()
            screenshots_dir = (directory / "screenshots").resolve()
            if image.is_file() and screenshots_dir in image.parents and image.suffix.lower() in IMAGE_EXTENSIONS:
                set_invisible(directory, image.name, image.name not in invisible_names(directory))
            self.redirect("/game/" + quote(relative))
            return
        if action == "scrape":
            self.scrape(relative, directory, values.get("duration", "300"))
            return
        if action == "archive":
            (directory / ".archived").touch()
            self.redirect("/")
            return
        if action == "delete":
            shutil.rmtree(directory)
            self.redirect("/")
            return
        path = directory / "game.md"
        _, sections, _ = split_sections(path.read_text(encoding="utf-8"))
        nodes = parse_nodes(sections.get("存档节点", ""))
        if action in {"node_add", "node_save", "node_delete"}:
            if action == "node_add":
                name = values.get("node_name", "").strip()
                if name:
                    nodes.append({"name": name, "file": values.get("node_file", ""), "description": values.get("node_description", "")})
            elif action == "node_save":
                index = int(values.get("node_index", "-1"))
                if 0 <= index < len(nodes):
                    nodes[index] = {"name": values.get("node_name", "").strip(), "file": values.get("node_file", ""), "description": values.get("node_description", "")}
            elif action == "node_delete":
                index = int(values.get("node_index", "-1"))
                if 0 <= index < len(nodes):
                    nodes.pop(index)
            replace_sections(path, {"存档节点": serialize_nodes(nodes)})
            self.redirect("/game/" + quote(relative))
            return
        name, platform = values.get("name", "").strip(), values.get("platform", "")
        save_dir = values.get("save_dir", "").strip()
        if not name or platform not in PLATFORMS:
            self.send_page("<p class='error'>游戏名和平台不能为空。</p>", HTTPStatus.BAD_REQUEST); return
        if save_dir and not Path(save_dir).is_dir():
            self.send_page("<p class='error'>存档目录不存在或不是目录。</p>", HTTPStatus.BAD_REQUEST); return
        target = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if target != directory and target.exists():
            self.send_page("<p class='error'>目标游戏已存在。</p>", HTTPStatus.CONFLICT); return
        basic = f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{save_dir or '没有'}"
        replace_sections(path, {"基本资料": basic, "简介": values.get("description", sections.get("简介", "")), "随记": values.get("note", sections.get("随记", ""))})
        if target != directory:
            target.parent.mkdir(parents=True, exist_ok=True)
            directory.rename(target)
            relative = str(target.relative_to(self.config["library_dir"]))
        self.redirect("/game/" + quote(relative))

    def scrape(self, relative: str, directory: Path, duration_value: str):
        try:
            duration = int(duration_value)
        except ValueError:
            duration = 300
        now = datetime.now().timestamp()
        source = self.config["screenshots_dir"]
        destination = directory / "screenshots"
        destination.mkdir(exist_ok=True)
        moved, ignored = [], []
        for item in source.iterdir():
            if not item.is_file():
                ignored.append(f"{item.name}（不是普通文件）")
                continue
            if item.suffix.lower() not in IMAGE_EXTENSIONS:
                ignored.append(f"{item.name}（不是支持的图片格式）")
                continue
            modified = item.stat().st_mtime
            if not now - duration <= modified <= now:
                ignored.append(f"{item.name}（不在时间范围内）")
                continue
            target = destination / item.name
            counter = 1
            while target.exists():
                target = destination / f"{item.stem}_{counter}{item.suffix}"
                counter += 1
            try:
                shutil.move(str(item), str(target))
                moved.append(target.name)
            except OSError as exc:
                ignored.append(f"{item.name}（移动失败：{exc}）")
        summary = [f"<h1>搜刮截图结果</h1><p>已移动 {len(moved)} 个文件。</p>"]
        if moved:
            summary.append("<h2>已移动</h2><ul>" + "".join(f"<li>{esc(name)}</li>" for name in moved) + "</ul>")
        else:
            summary.append("<p>没有找到截图</p>")
        if ignored:
            summary.append("<h2>已忽略</h2><ul>" + "".join(f"<li>{esc(name)}</li>" for name in ignored) + "</ul>")
        summary.append(f"<p><a href='/game/{quote(relative)}'>返回游戏</a></p>")
        self.send_page("".join(summary))

    def media(self, relative: str):
        path = (self.config["library_dir"] / relative).resolve()
        if not path.is_file() or self.config["library_dir"] not in path.parents:
            self.send_error(HTTPStatus.NOT_FOUND); return
        data = path.read_bytes(); self.send_response(HTTPStatus.OK); self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def frontend(self, relative: str = ""):
        path = (DIST_DIR / relative).resolve() if relative else DIST_DIR / "index.html"
        if not path.is_file() or DIST_DIR not in path.parents and path != DIST_DIR / "index.html":
            path = DIST_DIR / "index.html"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def main():
    config = load_config()
    clean_trash(config["library_dir"])
    Handler.config = config
    server = ThreadingHTTPServer(("127.0.0.1", 7007), Handler)
    server.timeout = 0.5
    watched = {path: path.stat().st_mtime for path in (Path(__file__).resolve(), Path(__file__).with_name("server.py")) if path.exists()}
    print("复古游戏博物馆运行于 http://127.0.0.1:7007")
    try:
        while True:
            server.handle_request()
            changed = any(path.exists() and path.stat().st_mtime != timestamp for path, timestamp in watched.items())
            if changed:
                module = importlib.reload(sys.modules[__name__])
                module.Handler.config = config
                server.RequestHandlerClass = module.Handler
                watched = {path: path.stat().st_mtime for path in (Path(__file__).resolve(), Path(__file__).with_name("server.py")) if path.exists()}
                print("已热加载代码")
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
