#!/usr/bin/env python3
"""Local web app for a personal retro game museum."""

from __future__ import annotations

import html
import importlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import traceback
from uuid import uuid4
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from server_logging import CountingWriter, RequestLogger, request_bytes

try:
    import yaml
except ImportError:  # Keep the error useful instead of failing during import.
    yaml = None

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
except ImportError:  # The repair tool reports a per-image error if optional image deps are absent.
    cv2 = None
    np = None
    Image = None
    ImageOps = None

CONFIG_PATH = Path.home() / ".config" / "game_museum" / "config.yaml"
PLATFORMS = ["NES", "SNES", "GB", "GBA", "N64", "PS1", "PS2", "PS3", "PSP", "NDS", "3DS", "Wii", "NGC", "NEO GEO", "Switch", "Arcade", "PC"]
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SECTION_NAMES = ["基本资料", "简介", "随记", "存档节点"]
TRASH_DIRNAME = ".trash"
DIST_DIR = Path(__file__).resolve().parent / "dist"


def fixed_stem(path: Path) -> bool:
    return path.stem.casefold().endswith("_fixed") or re.search(r"_fixed_\d+$", path.stem, re.IGNORECASE) is not None


def screenshot_base_name(name: str) -> str:
    return re.sub(r"_fixed(?:_\d+)?(?=\.(?:png|jpe?g)$)", "", name, flags=re.IGNORECASE)


def order_screenshots(directory: Path, images: list[Path]) -> list[Path]:
    order_file = directory / "screenshot_order.txt"
    names = [line.strip() for line in order_file.read_text(encoding="utf-8").splitlines() if line.strip()] if order_file.is_file() else []
    ranks = {name: index for index, name in enumerate(names)}
    fallback = len(ranks)
    return sorted(images, key=lambda image: (ranks.get(image.name, ranks.get(screenshot_base_name(image.name), fallback)), image.stat().st_mtime, image.name.casefold()))


def save_screenshot_order(directory: Path, names: list[str]) -> None:
    order_file = directory / "screenshot_order.txt"
    if names:
        order_file.write_text("\n".join(names) + "\n", encoding="utf-8")
    elif order_file.exists():
        order_file.unlink()


def trim_black_borders(image):
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    dark = gray < 70
    top, bottom, left, right = 0, image.shape[0], 0, image.shape[1]

    row_profile = dark.mean(axis=1)
    col_profile = dark.mean(axis=0)
    min_band = max(3, round(min(image.shape[:2]) * 0.004))

    def find_band(profile, start, step, limit):
        index = start
        while 0 <= index < limit:
            if profile[index] > 0.58:
                candidate = index
                while 0 <= candidate < limit and profile[candidate] > 0.58:
                    candidate += step
                if abs(candidate - index) >= min_band:
                    return candidate
                index = candidate
            else:
                index += step
        return start

    top = find_band(row_profile, 0, 1, len(row_profile))
    bottom = find_band(row_profile, len(row_profile) - 1, -1, len(row_profile)) + 1
    left = find_band(col_profile, 0, 1, len(col_profile))
    right = find_band(col_profile, len(col_profile) - 1, -1, len(col_profile)) + 1
    if bottom <= top or right <= left or (bottom - top) < 20 or (right - left) < 20:
        return image
    return image[top:bottom, left:right]


def enhance_screen(image):
    """Reduce camera scanlines/moire while keeping the original dimensions."""
    vertical_smoothed = cv2.GaussianBlur(image, (1, 5), 0)
    softened = cv2.addWeighted(image, 0.3, vertical_smoothed, 0.7, 0)
    denoised = cv2.bilateralFilter(softened, 5, 22, 22)
    detail = cv2.GaussianBlur(denoised, (0, 0), 1.1)
    return cv2.addWeighted(denoised, 1.22, detail, -0.22, 0)


def repair_image(source: Path) -> Path:
    if cv2 is None or np is None or Image is None:
        raise RuntimeError("未安装截图修复依赖，请运行 pip install -r requirements.txt")
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        pixels = np.asarray(image)
    height, width = pixels.shape[:2]
    scale = min(1.0, 1400.0 / max(height, width))
    small = cv2.resize(pixels, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else pixels
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    small_area = small.shape[0] * small.shape[1]
    candidates = []
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        area = cv2.contourArea(polygon)
        if len(polygon) != 4 or area < small_area * 0.12 or area > small_area * 0.98 or not cv2.isContourConvex(polygon):
            continue
        points = polygon.reshape(4, 2).astype(np.float32)
        sides = [np.linalg.norm(points[i] - points[(i + 1) % 4]) for i in range(4)]
        if min(sides) < min(small.shape[:2]) * 0.12:
            continue
        candidates.append((area, points))
    if not candidates:
        raise ValueError("未识别到屏幕边界")
    _, points = max(candidates, key=lambda candidate: candidate[0])
    sums, differences = points.sum(axis=1), np.diff(points, axis=1).ravel()
    ordered = np.array([points[np.argmin(sums)], points[np.argmin(differences)], points[np.argmax(sums)], points[np.argmax(differences)]], dtype=np.float32)
    ordered /= scale
    top_left, top_right, bottom_right, bottom_left = ordered
    output_width = max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left))
    output_height = max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left))
    output_width, output_height = max(1, round(output_width)), max(1, round(output_height))
    destination = np.array([[0, 0], [output_width - 1, 0], [output_width - 1, output_height - 1], [0, output_height - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    fixed = enhance_screen(trim_black_borders(cv2.warpPerspective(pixels, matrix, (output_width, output_height))))
    target = source.with_name(f"{source.stem}_fixed{source.suffix}")
    encoded = cv2.imencode(source.suffix.lower(), cv2.cvtColor(fixed, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95] if source.suffix.lower() in {".jpg", ".jpeg"} else [cv2.IMWRITE_PNG_COMPRESSION, 3])[1]
    target.write_bytes(encoded.tobytes())
    return target


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
    (result["library_dir"] / "inbox").mkdir(exist_ok=True)
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
    # The last recognized section extends to EOF; treating it as a suffix
    # would append its contents again every time the file is saved.
    suffix = ""
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


def save_directory(directory: Path) -> Path:
    save_dir = directory / "saves"
    save_dir.mkdir(exist_ok=True)
    return save_dir


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
        id_match = re.search(r"<!-- node-id: ([a-zA-Z0-9_-]+) -->", block)
        file_match = re.search(r"^- 存档文件：(.+)$", block, re.MULTILINE)
        description = re.sub(r"<!-- node-id: [a-zA-Z0-9_-]+ -->\n?", "", block, count=1).strip()
        description = re.sub(r"^- 存档文件：.+\n?", "", description, count=1).strip() if file_match else description
        nodes.append({"id": id_match.group(1) if id_match else f"legacy-{index}", "name": match.group(1).strip(), "file": file_match.group(1).strip() if file_match else "", "description": description})
    return nodes


def serialize_nodes(nodes: list[dict[str, str]]) -> str:
    blocks = []
    for node in nodes:
        block = f"### {node['name']}\n\n<!-- node-id: {node.get('id') or uuid4().hex} -->\n\n"
        if node.get("file"):
            block += f"- 存档文件：{node['file']}\n\n"
        if node.get("description"):
            block += node["description"].strip() + "\n\n"
        blocks.append(block.rstrip())
    return "\n\n".join(blocks)


def unique_nodes(nodes: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    seen = set()
    for node in nodes:
        key = (node.get("name", ""), node.get("file", ""), node.get("description", ""))
        if key not in seen:
            seen.add(key)
            result.append(node)
    return result


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
    label = "馆长模式" if mode == "curator" else "游客模式"
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>body{{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;line-height:1.6;color:#222}}a{{color:#1464a0}}input,select,textarea,button{{font:inherit;padding:.45rem;margin:.2rem 0}}textarea{{width:100%;min-height:8rem}}textarea.description{{min-height:3rem}}.game{{padding:.7rem 0;border-bottom:1px solid #ddd}}.muted{{color:#777}}.error{{color:#a00}}.shot{{display:inline-block;position:relative;margin:.4rem}}img.thumb{{max-width:180px;max-height:140px;display:block;object-fit:contain;cursor:pointer}}.trash{{position:absolute;right:0;top:0;border:0;background:#c62828;color:white;border-radius:50%;width:1.5rem;height:1.5rem;line-height:1rem;padding:0;cursor:pointer;font-weight:bold}}#mode{{margin-bottom:1rem}}.guest .admin-only{{display:none}}#viewer{{display:none;position:fixed;inset:0;background:#000d;align-items:center;justify-content:center;z-index:9999}}#viewer.open{{display:flex}}#viewer img{{max-width:85vw;max-height:85vh}}#viewer button{{position:fixed;background:#fff;border:0;font-size:2rem;cursor:pointer;z-index:10000}}#previous{{left:2vw}}#next{{right:2vw}}#close{{top:2vh;right:2vw}}</style></head><body class='{mode}'><nav id='mode'>{label} · <a href='/toggle-mode'>切换模式</a></nav>{content}<div id='viewer'><button type='button' id='previous' aria-label='上一张'>‹</button><img id='large-image' alt='截图大图'><button type='button' id='next' aria-label='下一张'>›</button><button type='button' id='close' aria-label='关闭'>×</button></div><script>const gallery=[...document.querySelectorAll('img.thumb')].map(image=>image.src);let current=0;const viewer=document.getElementById('viewer'),large=document.getElementById('large-image');function show(index){{if(!gallery.length)return;current=(index+gallery.length)%gallery.length;large.src=gallery[current];viewer.classList.add('open')}}document.querySelectorAll('img.thumb').forEach((image,index)=>image.onclick=()=>show(index));document.getElementById('previous').onclick=()=>show(current-1);document.getElementById('next').onclick=()=>show(current+1);document.getElementById('close').onclick=()=>viewer.classList.remove('open');viewer.onclick=event=>{{if(event.target===viewer)viewer.classList.remove('open')}};</script></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    config: dict[str, Path] = {}
    transfer_jobs = {}
    transfer_jobs_lock = threading.Lock()
    request_logger = RequestLogger(Path(__file__).resolve().parent, "game_museum")

    def handle_one_request(self):
        started_at = time.time()
        self._telemetry_status = HTTPStatus.INTERNAL_SERVER_ERROR
        original_wfile = self.wfile
        counted_wfile = CountingWriter(original_wfile)
        self.wfile = counted_wfile
        try:
            super().handle_one_request()
        finally:
            self.wfile = original_wfile
            try:
                self.request_logger.record(
                    method=getattr(self, "command", "UNKNOWN"),
                    target=getattr(self, "path", ""),
                    status=getattr(self, "_telemetry_status", HTTPStatus.INTERNAL_SERVER_ERROR),
                    request_size=request_bytes(self.headers),
                    response_size=counted_wfile.bytes_written,
                    started_at=started_at,
                )
            except Exception:
                pass

    def send_response(self, code, message=None):
        self._telemetry_status = int(code)
        super().send_response(code, message)

    def send_page(self, content: str, status=HTTPStatus.OK):
        mode = self.mode()
        if mode == "guest":
            content = "<style>.guest form:not(:has(input[name='q'])){display:block}.guest form:not(:has(input[name='q'])) textarea,.guest form:not(:has(input[name='q'])) button,.guest form:not(:has(input[name='q'])) input,.guest form:not(:has(input[name='q'])) select{display:none}</style><script>document.addEventListener('DOMContentLoaded',()=>{document.querySelectorAll('h2').forEach(heading=>{const title=heading.textContent.trim();if(title==='基本资料'||title==='存档文件'){heading.style.display='none';let item=heading.nextElementSibling;while(item && item.tagName!=='H2'){item.style.display='none';item=item.nextElementSibling}}if((title==='简介'||title==='随记')&&!heading.nextElementSibling?.nextElementSibling?.textContent.trim()){heading.style.display='none';let item=heading.nextElementSibling;while(item && item.tagName!=='H2'){item.style.display='none';item=item.nextElementSibling}}})})</script>" + content
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
            self.send_json({"games": [{"name": g["name"], "platform": g["platform"], "path": str(g["path"].relative_to(self.config["library_dir"]))} for g in games], "inbox_dir": str(self.config["library_dir"] / "inbox")})
            return
        if parsed.path == "/api/tools/transfer/status":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            with self.transfer_jobs_lock:
                job = self.transfer_jobs.get(job_id)
            if not job:
                self.send_json({"error": "找不到传输任务"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(job)
            return
        if parsed.path.startswith("/api/game/"):
            relative = unquote(parsed.path[len("/api/game/"):])
            directory = (self.config["library_dir"] / relative).resolve()
            if directory not in list(game_dirs(self.config["library_dir"])):
                self.send_json({"error": "没有找到游戏"}, HTTPStatus.NOT_FOUND)
                return
            data = game_data(directory)
            images = order_screenshots(directory, [p for p in (directory / "screenshots").glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS])
            creative_images = sorted((p for p in (directory / "creative").glob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS), key=lambda p: p.stat().st_mtime)
            hidden = invisible_names(directory)
            if self.mode() == "guest":
                images = [p for p in images if p.name not in hidden]
            save_dir = save_directory(directory)
            save_files = [p.name for p in save_dir.iterdir() if p.is_file()]
            self.send_json({"name": data["name"], "platform": data["platform"], "save_dir": str(save_dir), "description": data["description"], "note": data["note"], "images": [str(p.relative_to(self.config["library_dir"])) for p in images], "creative_images": [str(p.relative_to(self.config["library_dir"])) for p in creative_images], "hidden_images": sorted(hidden), "save_files": save_files, "nodes": unique_nodes(parse_nodes(data["sections"].get("存档节点", "")))})
            return
        if DIST_DIR.is_dir() and (parsed.path == "/" or parsed.path == "/new" or parsed.path == "/tools" or parsed.path.startswith("/game/") or parsed.path.startswith("/assets/")):
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
            if parsed.path == "/api/tools/transfer":
                self.transfer_api(json.loads(raw or "{}"))
                return
            if parsed.path == "/api/tools/delete-screenshots":
                self.delete_screenshots_api(json.loads(raw or "{}"))
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
        if not name or platform not in PLATFORMS:
            self.send_json({"error": "游戏名和平台不能为空"}, HTTPStatus.BAD_REQUEST); return
        directory = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if directory.exists():
            self.send_json({"error": "游戏已存在"}, HTTPStatus.CONFLICT); return
        directory.mkdir(parents=True); (directory / "screenshots").mkdir(); (directory / "creative").mkdir(); (directory / "saves").mkdir()
        replace_sections(directory / "game.md", {"基本资料": f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{directory / 'saves'}", "简介": str(values.get("description", "")), "随记": str(values.get("note", ""))})
        self.send_json({"path": str(directory.relative_to(self.config["library_dir"]))}, HTTPStatus.CREATED)

    def transfer_api(self, values: dict):
        device = values.get("device") or {}
        source_value = str(values.get("source", "")).strip()
        destination = str(values.get("destination", "")).strip()
        kind = str(values.get("kind", "ROM"))
        if kind not in {"ROM", "截图"} or not isinstance(device, dict) or not source_value or not destination:
            self.send_json({"error": "设备、本地路径和目标路径不能为空"}, HTTPStatus.BAD_REQUEST); return
        local_path = Path(source_value).expanduser()
        if kind == "截图" and not local_path.is_dir():
            self.send_json({"error": f"本地保存目录不存在：{local_path}"}, HTTPStatus.BAD_REQUEST); return
        if kind == "ROM" and not local_path.exists():
            self.send_json({"error": f"本地路径不存在：{local_path}"}, HTTPStatus.BAD_REQUEST); return
        host = str(device.get("ip", "")).strip()
        user = str(device.get("user", "root")).strip() or "root"
        try:
            port = int(device.get("port", 22))
        except (TypeError, ValueError):
            port = 22
        if not host:
            self.send_json({"error": "设备 IP 不能为空"}, HTTPStatus.BAD_REQUEST); return
        if shutil.which("rsync") is None:
            self.send_json({"error": "本机未安装 rsync"}, HTTPStatus.BAD_REQUEST); return
        password = str(device.get("password", ""))
        if not password:
            self.send_json({"error": "设备未配置 SSH 密码，请先在设备配置中修改"}, HTTPStatus.BAD_REQUEST); return
        if shutil.which("sshpass") is None:
            self.send_json({"error": "本机未安装 sshpass，无法使用设备密码认证"}, HTTPStatus.BAD_REQUEST); return
        remote_path = f"{user}@{host}:{destination}"
        ssh_options = f"ssh -p {port} -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2 -o StrictHostKeyChecking=accept-new"
        command = ["sshpass", "-e", "rsync", "-avh", "--progress", "--stats", "--out-format=__GAME_MUSEUM_FILE__%i %n", "-e", ssh_options]
        command += [remote_path, str(local_path)] if kind == "截图" else [str(local_path), remote_path]
        job_id = uuid4().hex
        with self.transfer_jobs_lock:
            self.transfer_jobs[job_id] = {"state": "running", "completed": 0, "message": "传输中：已完成 0 个文件"}
        threading.Thread(target=self.run_transfer, args=(job_id, command, password), daemon=True).start()
        self.send_json({"job_id": job_id}, HTTPStatus.ACCEPTED)

    def run_transfer(self, job_id: str, command: list[str], password: str) -> None:
        environment = os.environ.copy()
        environment["SSHPASS"] = password
        completed = 0
        output = []
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL, env=environment, bufsize=1)
            started = time.monotonic()
            assert process.stdout is not None
            for line in process.stdout:
                output.append(line)
                if line.startswith("__GAME_MUSEUM_FILE__"):
                    itemized = line[len("__GAME_MUSEUM_FILE__"):].lstrip()
                    if not itemized.startswith("d"):
                        completed += 1
                        with self.transfer_jobs_lock:
                            self.transfer_jobs[job_id].update(completed=completed, message=f"传输中：已完成 {completed} 个文件")
                if time.monotonic() - started > 600:
                    process.kill()
                    raise subprocess.TimeoutExpired(command, 600)
            returncode = process.wait()
            if returncode != 0:
                detail = [line.strip() for line in output if line.strip()][-1:]
                result = {"state": "error", "completed": completed, "error": detail[0] if detail else "SSH 传输失败"}
            else:
                result = {"state": "done", "completed": completed, "message": f"传输 {completed} 个文件完成"}
        except subprocess.TimeoutExpired:
            result = {"state": "error", "completed": completed, "error": "传输超时"}
        except Exception as exc:
            result = {"state": "error", "completed": completed, "error": str(exc)}
        with self.transfer_jobs_lock:
            self.transfer_jobs[job_id] = result

    def delete_screenshots_api(self, values: dict) -> None:
        device = values.get("device") or {}
        remote_dir = str(values.get("destination", "")).strip()
        password = str(device.get("password", "")) if isinstance(device, dict) else ""
        host = str(device.get("ip", "")).strip() if isinstance(device, dict) else ""
        user = str(device.get("user", "root")).strip() or "root" if isinstance(device, dict) else "root"
        try:
            port = int(device.get("port", 22))
        except (TypeError, ValueError):
            port = 22
        if not remote_dir.startswith("/") or not host or not password:
            self.send_json({"error": "设备配置或截图目录无效"}, HTTPStatus.BAD_REQUEST); return
        if shutil.which("sshpass") is None:
            self.send_json({"error": "本机未安装 sshpass，无法使用设备密码认证"}, HTTPStatus.BAD_REQUEST); return
        ssh_options = ["-p", str(port), "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
        delete_command = f"find {shlex.quote(remote_dir)} -maxdepth 1 -type f -iname '*.png' -delete"
        command = ["sshpass", "-e", "ssh", *ssh_options, f"{user}@{host}", delete_command]
        environment = os.environ.copy()
        environment["SSHPASS"] = password
        try:
            result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=environment, timeout=60)
        except subprocess.TimeoutExpired:
            self.send_json({"error": "删除超时"}, HTTPStatus.GATEWAY_TIMEOUT); return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:]
            self.send_json({"error": detail[0] if detail else "删除失败"}, HTTPStatus.BAD_GATEWAY); return
        self.send_json({"ok": True, "message": "已从设备删除 PNG 截图"})

    def update_api(self, relative: str, values: dict):
        directory = (self.config["library_dir"] / relative).resolve()
        if directory not in list(game_dirs(self.config["library_dir"])):
            self.send_json({"error": "没有找到游戏"}, HTTPStatus.NOT_FOUND); return
        path = directory / "game.md"
        _, sections, _ = split_sections(path.read_text(encoding="utf-8"))
        save_dir = save_directory(directory)
        action = values.get("action", "save")
        if action == "archive":
            (directory / ".archived").touch(); self.send_json({"ok": True}); return
        if action == "delete":
            shutil.rmtree(directory); self.send_json({"ok": True}); return
        if action == "repair_images":
            results = []
            screenshots = (directory / "screenshots").resolve()
            selected = values.get("images", [])
            if not isinstance(selected, list):
                self.send_json({"error": "图片选择格式错误"}, HTTPStatus.BAD_REQUEST); return
            for relative_image in selected:
                source = (self.config["library_dir"] / str(relative_image)).resolve()
                result = {"source": str(relative_image)}
                if screenshots not in source.parents or not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
                    result["error"] = "图片不存在或不是支持的截图格式"
                elif fixed_stem(source):
                    result["error"] = "已是修复图片"
                else:
                    try:
                        result["output"] = str(repair_image(source).relative_to(self.config["library_dir"]))
                        result["ok"] = True
                    except Exception as exc:
                        result["error"] = str(exc)
                results.append(result)
            self.send_json({"ok": True, "results": results}); return
        if action == "reorder_images":
            order = values.get("order", [])
            screenshots = (directory / "screenshots").resolve()
            if not isinstance(order, list) or any((self.config["library_dir"] / str(item)).resolve().parent != screenshots or not (self.config["library_dir"] / str(item)).is_file() for item in order):
                self.send_json({"error": "截图排序数据无效"}, HTTPStatus.BAD_REQUEST); return
            save_screenshot_order(directory, [Path(str(item)).name for item in order])
            self.send_json({"ok": True}); return
        if action == "delete_fixed_image":
            image = (self.config["library_dir"] / str(values.get("image", ""))).resolve()
            screenshots = (directory / "screenshots").resolve()
            if screenshots not in image.parents or not image.is_file() or not fixed_stem(image):
                self.send_json({"error": "修复图片不存在"}, HTTPStatus.NOT_FOUND); return
            image.unlink()
            order_file = directory / "screenshot_order.txt"
            if order_file.is_file():
                save_screenshot_order(directory, [name for name in order_file.read_text(encoding="utf-8").splitlines() if name.strip() and name.strip() != image.name])
            self.send_json({"ok": True}); return
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
                order_file = directory / "screenshot_order.txt"
                if order_file.is_file():
                    save_screenshot_order(directory, [name for name in order_file.read_text(encoding="utf-8").splitlines() if name.strip() and name.strip() != image.name])
            self.send_json({"ok": True}); return
        if action in {"node_add", "node_save", "node_delete"}:
            nodes = parse_nodes(sections.get("存档节点", ""))
            nodes = unique_nodes(nodes)
            index = int(values.get("index", -1))
            node_id = str(values.get("id", ""))
            node_file = str(values.get("file", ""))
            if action != "node_delete" and node_file and node_file not in {p.name for p in save_dir.iterdir() if p.is_file()}:
                self.send_json({"error": "存档文件不存在"}, HTTPStatus.BAD_REQUEST); return
            if action == "node_delete":
                target = next((position for position, node in enumerate(nodes) if node.get("id") == node_id), index)
                if 0 <= target < len(nodes):
                    nodes.pop(target)
            elif action == "node_add":
                new_node = {"id": uuid4().hex, "name": str(values.get("name", "")).strip(), "file": node_file, "description": str(values.get("description", ""))}
                if new_node not in nodes:
                    nodes.append(new_node)
            else:
                target = next((position for position, node in enumerate(nodes) if node.get("id") == node_id), index)
                if 0 <= target < len(nodes):
                    nodes[target] = {"id": nodes[target].get("id", node_id), "name": str(values.get("name", "")).strip(), "file": node_file, "description": str(values.get("description", ""))}
            replace_sections(path, {"存档节点": serialize_nodes(nodes)})
            self.send_json({"ok": True, "nodes": nodes}); return
        name, platform = str(values.get("name", "")).strip(), values.get("platform", "")
        if not name or platform not in PLATFORMS:
            self.send_json({"error": "游戏名和平台不能为空"}, HTTPStatus.BAD_REQUEST); return
        target = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if target != directory and target.exists():
            self.send_json({"error": "目标游戏已存在"}, HTTPStatus.CONFLICT); return
        basic = f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{target / 'saves'}"
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
            self.send_page(f"<h1>新建游戏</h1><form class='admin-only' method='post' action='/games/new'><p>游戏名<br><input name='name' required></p><p>平台<br><select name='platform'>{options}</select></p><p>简介<br><textarea name='description'></textarea></p><button>保存</button> <a href='/'>取消</a></form>")
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
        save_dir = save_directory(directory)
        platform_options = "".join(f"<option {'selected' if p == data['platform'] else ''}>{esc(p)}</option>" for p in PLATFORMS)
        ranges = {"300": "最近 5 分钟", "600": "最近 10 分钟", "1800": "最近 30 分钟", "3600": "最近 60 分钟", "18000": "最近 5 小时"}
        range_options = "".join(f"<option value='{seconds}'>{label}</option>" for seconds, label in ranges.items())
        nodes = unique_nodes(parse_nodes(data['sections'].get('存档节点', '')))
        save_files = [p for p in save_dir.iterdir() if p.is_file()]
        file_options = "<option value=''>没有关联存档文件</option>" + "".join(f"<option value='{esc(str(p))}'>{esc(p.name)}</option>" for p in save_files)
        node_cards = []
        for index, node in enumerate(nodes):
            options = "<option value=''>没有关联存档文件</option>" + "".join(f"<option value='{esc(str(p))}' {'selected' if str(p) == node['file'] else ''}>{esc(p.name)}</option>" for p in save_files)
            node_cards.append(f"<details><summary>{esc(node['name'])}</summary><form class='admin-only' method='post'><input type='hidden' name='node_index' value='{index}'><p>节点名称<br><input name='node_name' value='{esc(node['name'])}' required></p><p>存档文件<br><select name='node_file'>{options}</select></p><p>说明<br><textarea name='node_description'>{esc(node['description'])}</textarea></p><button name='action' value='node_save'>保存节点</button> <button name='action' value='node_delete'>删除节点</button></form></details>")
        node_html = "".join(node_cards)
        add_node = f"<form method='post'><h3>保存节点</h3><p>节点名称<br><input name='node_name' required></p><p>存档文件<br><select name='node_file'>{file_options}</select></p><p>说明<br><textarea name='node_description'></textarea></p><button name='action' value='node_add'>保存节点</button></form>"
        save_html = "".join(f"<li>{esc(p.name)} <button type='button' onclick=\"navigator.clipboard.writeText({json.dumps(str(p))})\">复制路径</button></li>" for p in save_files)
        self.send_page(f"<p><a href='/'>← 游戏列表</a></p><h1>{esc(data['name'])} <small>{esc(data['platform'])}</small></h1><form method='post'><h2>基本资料</h2><p>游戏名<br><input name='name' value='{esc(data['name'])}' required></p><p>平台<br><select name='platform'>{platform_options}</select></p><h2>简介</h2><textarea class='description' name='description'>{esc(data['description'])}</textarea><div>{render_markdown(data['description'])}</div><h2>随记</h2><textarea name='note'>{esc(data['note'])}</textarea><div>{render_markdown(data['note'])}</div><p><button name='action' value='save'>保存</button></p></form><form method='post'><button name='action' value='archive'>归档</button> <button name='action' value='delete' onclick=\"return confirm('确定删除资料、截图和存档？ROM 不会被删除。')\">删除</button></form><h2>截图</h2><form method='post'><select name='duration'>{range_options}</select><button name='action' value='scrape'>搜刮截图</button></form>{image_html}<h2>存档文件</h2><ul>{save_html}</ul><h2>存档节点</h2>{node_html}{add_node}")

    def create(self, values):
        name, platform = values.get("name", "").strip(), values.get("platform", "")
        if not name or platform not in PLATFORMS:
            self.send_page("<p class='error'>游戏名和平台不能为空。</p>", HTTPStatus.BAD_REQUEST); return
        directory = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if directory.exists():
            self.send_page("<p class='error'>游戏已存在。</p>", HTTPStatus.CONFLICT); return
        directory.mkdir(parents=True); (directory / "screenshots").mkdir(); (directory / "creative").mkdir(); (directory / "saves").mkdir()
        basic = f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{directory / 'saves'}"
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
        if not name or platform not in PLATFORMS:
            self.send_page("<p class='error'>游戏名和平台不能为空。</p>", HTTPStatus.BAD_REQUEST); return
        target = self.config["library_dir"] / safe_name(platform) / safe_name(name)
        if target != directory and target.exists():
            self.send_page("<p class='error'>目标游戏已存在。</p>", HTTPStatus.CONFLICT); return
        basic = f"- 游戏名：{name}\n- 平台：{platform}\n- 存档目录：{target / 'saves'}"
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
