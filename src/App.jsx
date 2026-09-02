import { useEffect, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { Eye, EyeOff, Trash2 } from "lucide-react";

const PLATFORMS = ["NES", "SNES", "GB", "GBA", "N64", "PS1", "PS2", "Arcade"];

function markdown(value) {
  return { __html: DOMPurify.sanitize(marked.parse(value || "")) };
}

function defaultMode() {
  const saved = document.cookie.match(/(?:^|;\s*)museum_mode=(guest|curator)/);
  if (saved) return saved[1];
  return /mobile|android|iphone|ipad/i.test(navigator.userAgent) ? "guest" : "curator";
}

function ModeSwitch({ mode, setMode }) {
  const toggle = () => {
    const next = mode === "curator" ? "guest" : "curator";
    document.cookie = `museum_mode=${next}; Path=/; SameSite=Lax`;
    setMode(next);
  };
  return <nav className="mode-switch">{mode === "curator" ? "馆长模式" : "老总模式"} · <button onClick={toggle}>切换模式</button></nav>;
}

export default function App() {
  const [games, setGames] = useState([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [detail, setDetail] = useState(null);
  const [mode, setMode] = useState(defaultMode);
  const [creating, setCreating] = useState(window.location.pathname === "/new");
  const gamePath = window.location.pathname.startsWith("/game/") ? decodeURIComponent(window.location.pathname.slice(6)) : null;

  useEffect(() => {
    fetch(gamePath ? `/api/game/${encodeURIComponent(gamePath)}` : "/api/games")
      .then((response) => { if (!response.ok) throw new Error("无法读取游戏资料"); return response.json(); })
      .then((data) => gamePath ? setDetail(data) : setGames(data.games))
      .catch((reason) => setError(reason.message));
  }, [gamePath]);

  if (creating) return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><h1>新建游戏</h1><CreateGame onDone={(path) => { window.history.pushState({}, "", `/game/${encodeURIComponent(path)}`); window.location.reload(); }} /></main>;
  if (detail) return <GameDetail detail={detail} setDetail={setDetail} mode={mode} setMode={setMode} gamePath={gamePath} />;

  const filtered = games.filter((game) => game.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><header><h1>复古游戏博物馆</h1><input placeholder="搜索游戏名" value={query} onChange={(event) => setQuery(event.target.value)} /></header>{mode === "curator" && <p><a href="/new">新建游戏</a></p>}{error && <p className="error">{error}</p>}{!error && filtered.length === 0 && <p className="muted">{query ? "没有找到游戏" : "没有游戏"}</p>}<section className="game-list">{filtered.map((game) => <a className="game-card" href={`/game/${encodeURIComponent(game.path)}`} key={game.path}><strong>{game.name}</strong><span>{game.platform}</span></a>)}</section></main>;
}

function CreateGame({ onDone }) {
  const [form, setForm] = useState({ name: "", platform: "NES", save_dir: "", description: "" });
  const [error, setError] = useState("");
  const update = (event) => setForm({ ...form, [event.target.name]: event.target.value });
  const submit = async (event) => { event.preventDefault(); const response = await fetch("/api/games", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(form) }); const data = await response.json(); if (!response.ok) setError(data.error); else onDone(data.path); };
  return <form onSubmit={submit}><p>游戏名<br /><input name="name" required value={form.name} onChange={update} /></p><p>平台<br /><select name="platform" value={form.platform} onChange={update}>{PLATFORMS.map((p) => <option key={p}>{p}</option>)}</select></p><p>存档目录路径（可选）<br /><input name="save_dir" value={form.save_dir} onChange={update} /></p><p>简介<br /><textarea name="description" value={form.description} onChange={update} /></p>{error && <p className="error">{error}</p>}<button>保存</button> <a href="/">取消</a></form>;
}

function GameDetail({ detail, setDetail, mode, setMode, gamePath }) {
  const [duration, setDuration] = useState("300");
  const [lightbox, setLightbox] = useState(null);
  const galleryImages = [...detail.images, ...(detail.creative_images || [])];
  const save = async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...data, action: "save" }) }); if (response.ok) setDetail({ ...detail, ...data }); };
  const toggleImage = async (image) => { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "toggle_visibility", image }) }); if (response.ok) { const name = image.split("/").pop(); const hidden = new Set(detail.hidden_images || []); hidden.has(name) ? hidden.delete(name) : hidden.add(name); setDetail({ ...detail, hidden_images: [...hidden] }); } };
  const deleteImage = async (image) => { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete_image", image }) }); if (response.ok) setDetail({ ...detail, images: detail.images.filter((item) => item !== image) }); };
  const archive = async () => { if (confirm("确定归档这个游戏？")) { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "archive" }) }); if (response.ok) window.location.href = "/"; } };
  const remove = async () => { if (confirm("确定删除资料、截图和存档？ROM 不会被删除。")) { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete" }) }); if (response.ok) window.location.href = "/"; } };
  const scrape = async () => { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "scrape", duration }) }); if (response.ok) { const data = await response.json(); setDetail({ ...detail, images: [...detail.images, ...data.moved] }); } };
  useEffect(() => {
    if (lightbox === null) return undefined;
    const onKey = (event) => {
      if (event.key === "Escape") setLightbox(null);
      if (event.key === "ArrowLeft") setLightbox((current) => galleryImages[(galleryImages.indexOf(current) - 1 + galleryImages.length) % galleryImages.length]);
      if (event.key === "ArrowRight") setLightbox((current) => galleryImages[(galleryImages.indexOf(current) + 1) % galleryImages.length]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox, detail.images, detail.creative_images]);
  const changeLightbox = (step) => setLightbox((current) => { const index = galleryImages.indexOf(current); return galleryImages[(index + step + galleryImages.length) % galleryImages.length]; });
  const renderImage = (image, editable = false) => <div className="image-card" key={image}><img className="museum-image" src={`/media/${encodeURIComponent(image)}`} onClick={() => setLightbox(image)} />{editable && <span className="image-actions"><button title="切换显示" onClick={() => toggleImage(image)}>{(detail.hidden_images || []).includes(image.split("/").pop()) ? <EyeOff size={16} /> : <Eye size={16} />}</button><button title="移到垃圾桶" onClick={() => deleteImage(image)}><Trash2 size={16} /></button></span>}</div>;
  return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><a href="/">← 游戏列表</a><h1>{detail.name} <small>{detail.platform}</small></h1>{mode === "curator" && <form onSubmit={save}><p>游戏名<br /><input name="name" defaultValue={detail.name} /></p><p>平台<br /><select name="platform" defaultValue={detail.platform}>{PLATFORMS.map((p) => <option key={p}>{p}</option>)}</select></p><p>存档目录路径（可选）<br /><input name="save_dir" defaultValue={detail.save_dir} /></p><h2>简介</h2><textarea name="description" defaultValue={detail.description} /><h2>随记</h2><textarea name="note" defaultValue={detail.note} /><button>保存</button></form>}<section><h2>简介</h2>{detail.description && <div className="markdown" dangerouslySetInnerHTML={markdown(detail.description)} />}<h2>随记</h2>{detail.note && <div className="markdown" dangerouslySetInnerHTML={markdown(detail.note)} />}</section>{(detail.creative_images || []).length > 0 && <section><h2>二创</h2><div className="image-grid">{detail.creative_images.map((image) => renderImage(image))}</div></section>}<section><h2>截图</h2>{mode === "curator" && <p><select value={duration} onChange={(event) => setDuration(event.target.value)}><option value="300">最近 5 分钟</option><option value="600">最近 10 分钟</option><option value="1800">最近 30 分钟</option><option value="3600">最近 60 分钟</option><option value="18000">最近 5 小时</option></select><button onClick={scrape}>搜刮截图</button></p>}<div className="image-grid">{detail.images.map((image) => renderImage(image, mode === "curator"))}</div></section>{mode === "curator" && <><NodeEditor detail={detail} setDetail={setDetail} gamePath={gamePath} mode={mode} /><p><button onClick={archive}>归档</button> <button onClick={remove}>删除</button></p></>}{lightbox && <div className="lightbox" onClick={() => setLightbox(null)}><button onClick={(event) => { event.stopPropagation(); changeLightbox(-1); }}>‹</button><img src={`/media/${encodeURIComponent(lightbox)}`} onClick={(event) => event.stopPropagation()} /><button onClick={(event) => { event.stopPropagation(); changeLightbox(1); }}>›</button></div>}</main>;
}

function NodeEditor({ detail, setDetail, gamePath, mode }) {
  if (mode !== "curator") return null;
  const submit = async (event, action, index = -1, form = event.currentTarget) => { event.preventDefault(); const data = Object.fromEntries(new FormData(form)); const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...data, action, index }) }); if (response.ok) setDetail({ ...detail, nodes: (await response.json()).nodes }); };
  return <section><h2>存档节点</h2>{detail.nodes.map((node, index) => <form className="node-form" key={`${index}-${node.name}`} onSubmit={(event) => submit(event, "node_save", index)}><input name="name" defaultValue={node.name} required /><select name="file" defaultValue={node.file || ""}><option value="">没有关联存档文件</option>{detail.save_files.map((file) => <option key={file}>{file}</option>)}</select><textarea name="description" defaultValue={node.description} /><button>保存节点</button><button type="button" onClick={(event) => submit(event, "node_delete", index, event.currentTarget.form)}>删除节点</button></form>)}<form className="node-form" onSubmit={(event) => submit(event, "node_add")}><input name="name" placeholder="节点名称" required /><select name="file"><option value="">没有关联存档文件</option>{detail.save_files.map((file) => <option key={file}>{file}</option>)}</select><textarea name="description" placeholder="说明" /><button>新增节点</button></form></section>;
}
