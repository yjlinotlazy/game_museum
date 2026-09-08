import { useEffect, useState } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { Eye, EyeOff, Trash2, Wrench, Plus, Send, Terminal, Server } from "lucide-react";

const PLATFORMS = ["NES", "SNES", "GB", "GBA", "N64", "PS1", "PS2", "NDS", "3DS", "Arcade"];

function markdown(value) {
  return { __html: DOMPurify.sanitize(marked.parse(value || "")) };
}

function isFixedImage(image) {
  return /_fixed(?:_\d+)?\.(?:png|jpe?g)$/i.test(image);
}

function screenshotKey(image) {
  return image.replace(/_fixed(?:_\d+)?(?=\.(?:png|jpe?g)$)/i, "");
}

function preferredScreenshots(images) {
  const fixedKeys = new Set(images.filter(isFixedImage).map(screenshotKey));
  return images.filter((image) => isFixedImage(image) || !fixedKeys.has(screenshotKey(image)));
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
  return <nav className="mode-switch">{mode === "curator" ? "馆长模式" : "游客模式"} · <button onClick={toggle}>切换模式</button></nav>;
}

export default function App() {
  const [games, setGames] = useState([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [detail, setDetail] = useState(null);
  const [mode, setMode] = useState(defaultMode);
  const [creating, setCreating] = useState(window.location.pathname === "/new");
  const toolbox = window.location.pathname === "/tools";
  const rawGamePath = window.location.pathname.startsWith("/game/") ? window.location.pathname.slice(6) : null;
  const tools = Boolean(rawGamePath && rawGamePath.endsWith("/tools"));
  const gamePath = rawGamePath ? decodeURIComponent(tools ? rawGamePath.slice(0, -6) : rawGamePath) : null;

  useEffect(() => {
    fetch(gamePath ? `/api/game/${encodeURIComponent(gamePath)}` : "/api/games")
      .then((response) => { if (!response.ok) throw new Error("无法读取游戏资料"); return response.json(); })
      .then((data) => gamePath ? setDetail(data) : setGames(data.games))
      .catch((reason) => setError(reason.message));
  }, [gamePath, mode]);

  if (creating) return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><h1>新建游戏</h1><CreateGame onDone={(path) => { window.history.pushState({}, "", `/game/${encodeURIComponent(path)}`); window.location.reload(); }} /></main>;
  if (toolbox) return <Toolbox mode={mode} setMode={setMode} />;
  if (detail) return tools ? <RepairTool detail={detail} setDetail={setDetail} mode={mode} setMode={setMode} gamePath={gamePath} /> : <GameDetail detail={detail} setDetail={setDetail} mode={mode} setMode={setMode} gamePath={gamePath} />;

  const filtered = games.filter((game) => game.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} />{mode === "curator" && <p className="top-tools"><a href="/tools">馆长的工具箱</a></p>}<header><h1>复古游戏博物馆</h1><input placeholder="搜索游戏名" value={query} onChange={(event) => setQuery(event.target.value)} /></header>{mode === "curator" && <p><a href="/new">新建游戏</a></p>}{error && <p className="error">{error}</p>}{!error && filtered.length === 0 && <p className="muted">{query ? "没有找到游戏" : "没有游戏"}</p>}<section className="game-list">{filtered.map((game) => <a className="game-card" href={`/game/${encodeURIComponent(game.path)}`} key={game.path}><strong>{game.name}</strong><span>{game.platform}</span></a>)}</section></main>;
}

function TerminalPathInput({ label, value, onChange, suggestions = [], required = true }) {
  const listId = `${label}-path-suggestions`;
  return <label className="terminal-field"><span>{label}</span><div className="terminal-input"><Terminal size={16} /><input required={required} list={listId} value={value} onChange={(event) => onChange(event.target.value)} placeholder="/path/to/file-or-folder" /><datalist id={listId}>{suggestions.map((item) => <option key={item} value={item} />)}</datalist></div></label>;
}

function Toolbox({ mode, setMode }) {
  const [devices, setDevices] = useState(() => JSON.parse(localStorage.getItem("museum_devices") || "[]"));
  const [device, setDevice] = useState(null);
  const [name, setName] = useState("");
  const [ip, setIp] = useState("");
  const [source, setSource] = useState("");
  const [destination, setDestination] = useState("/mnt/mmc/ROMS/");
  const [kind, setKind] = useState("ROM");
  const [status, setStatus] = useState("");
  const suggestions = JSON.parse(localStorage.getItem("museum_path_history") || "[]");
  const saveDevices = (next) => { setDevices(next); localStorage.setItem("museum_devices", JSON.stringify(next)); };
  const addDevice = (event) => { event.preventDefault(); if (!name.trim() || !ip.trim()) return; const next = [...devices, { name: name.trim(), ip: ip.trim(), user: "root", port: 22 }]; saveDevices(next); setDevice(next.at(-1)); setName(""); setIp(""); };
  const transfer = async (event) => {
    event.preventDefault();
    if (!device) { setStatus("请先选择设备"); return; }
    setStatus("传输中…");
    localStorage.setItem("museum_path_history", JSON.stringify([...new Set([source, destination, ...suggestions].filter(Boolean))].slice(0, 20)));
    const response = await fetch("/api/tools/transfer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device, source, destination, kind }) });
    const data = await response.json();
    setStatus(response.ok ? (data.message || "传输完成") : (data.error || "传输失败"));
  };
  if (mode !== "curator") return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><p>馆长的工具箱仅在馆长模式下可用。</p><a href="/">返回游戏列表</a></main>;
  return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><p><a href="/">← 游戏列表</a></p><h1>馆长的工具箱</h1><p className="toolbox-intro">通过 SSH 手动把文件传到已配置的设备。</p><section className="toolbox-panel"><h2><Server size={21} /> 设备</h2>{devices.length > 0 && <div className="device-list">{devices.map((item) => <button type="button" className={device?.ip === item.ip ? "device selected" : "device"} key={`${item.name}-${item.ip}`} onClick={() => setDevice(item)}><strong>{item.name}</strong><span>{item.user}@{item.ip}:{item.port}</span></button>)}</div>}<form className="device-form" onSubmit={addDevice}><input required placeholder="设备名称" value={name} onChange={(event) => setName(event.target.value)} /><input required placeholder="IP 地址（可先填占位值）" value={ip} onChange={(event) => setIp(event.target.value)} /><button><Plus size={17} /> 添加设备</button></form></section><section className="toolbox-panel"><h2><Send size={21} /> 传输文件</h2><div className="transfer-tabs"><button type="button" className={kind === "ROM" ? "active" : ""} onClick={() => setKind("ROM")}>传 ROM</button><button type="button" className={kind === "截图" ? "active" : ""} onClick={() => setKind("截图")}>传截图</button></div><form onSubmit={transfer}><TerminalPathInput label={`${kind} 本地路径`} value={source} onChange={setSource} suggestions={suggestions} /><TerminalPathInput label="设备目标路径" value={destination} onChange={setDestination} suggestions={["/mnt/mmc/ROMS/", "/mnt/mmc/ROMS/NDS/", "/mnt/mmc/ROMS/3DS/", ...suggestions]} /><button disabled={!device || !source}><Send size={17} /> 传到 {device?.name || "设备"}</button>{status && <p className="transfer-status">{status}</p>}</form></section></main>;
}

function CreateGame({ onDone }) {
  const [form, setForm] = useState({ name: "", platform: "NES", save_dir: "", description: "" });
  const [error, setError] = useState("");
  const update = (event) => setForm({ ...form, [event.target.name]: event.target.value });
  const submit = async (event) => { event.preventDefault(); const response = await fetch("/api/games", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(form) }); const data = await response.json(); if (!response.ok) setError(data.error); else onDone(data.path); };
  return <form onSubmit={submit}><p>游戏名<br /><input name="name" required value={form.name} onChange={update} /></p><p>平台<br /><select name="platform" value={form.platform} onChange={update}>{PLATFORMS.map((p) => <option key={p}>{p}</option>)}</select></p><p>存档目录路径（可选）<br /><input name="save_dir" value={form.save_dir} onChange={update} /></p><p>简介<br /><textarea name="description" value={form.description} onChange={update} /></p>{error && <p className="error">{error}</p>}<button>保存</button> <a href="/">取消</a></form>;
}

function RepairTool({ detail, setDetail, mode, setMode, gamePath }) {
  const candidates = detail.images.filter((image) => !/_fixed(?:_\d+)?\.(?:png|jpe?g)$/i.test(image));
  const fixedImages = detail.images.filter((image) => /_fixed(?:_\d+)?\.(?:png|jpe?g)$/i.test(image));
  const galleryImages = [...candidates, ...fixedImages];
  const [selected, setSelected] = useState([]);
  const [selectedFixed, setSelectedFixed] = useState([]);
  const [lightbox, setLightbox] = useState(null);
  const [results, setResults] = useState([]);
  const [working, setWorking] = useState(false);
  const toggle = (image) => setSelected((current) => current.includes(image) ? current.filter((item) => item !== image) : [...current, image]);
  const toggleFixed = (image) => setSelectedFixed((current) => current.includes(image) ? current.filter((item) => item !== image) : [...current, image]);
  const repair = async () => {
    setWorking(true);
    const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "repair_images", images: selected }) });
    const data = await response.json();
    if (response.ok) {
      setResults(data.results);
      const created = data.results.filter((item) => item.ok).map((item) => item.output);
      setDetail({ ...detail, images: [...detail.images, ...created] });
      setSelected([]);
    } else setResults([{ source: "", error: data.error || "修复失败" }]);
    setWorking(false);
  };
  const deleteResult = async (output) => {
    const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete_fixed_image", image: output }) });
    if (response.ok) {
      setResults((current) => current.filter((result) => result.output !== output));
      setDetail({ ...detail, images: detail.images.filter((image) => image !== output) });
    }
  };
  const deleteFixed = async () => {
    if (!selectedFixed.length || !confirm(`确定永久删除选中的 ${selectedFixed.length} 张修复图片？`)) return;
    const responses = await Promise.all(selectedFixed.map((image) => fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete_fixed_image", image }) })));
    const deleted = selectedFixed.filter((_, index) => responses[index].ok);
    setDetail({ ...detail, images: detail.images.filter((image) => !deleted.includes(image)) });
    setSelectedFixed([]);
  };
  useEffect(() => {
    if (lightbox === null) return undefined;
    const onKey = (event) => {
      if (event.key === "Escape") setLightbox(null);
      if (event.key === "ArrowLeft") setLightbox((current) => galleryImages[(galleryImages.indexOf(current) - 1 + galleryImages.length) % galleryImages.length]);
      if (event.key === "ArrowRight") setLightbox((current) => galleryImages[(galleryImages.indexOf(current) + 1) % galleryImages.length]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox, detail.images]);
  useEffect(() => {
    const preventCardToggle = (event) => {
      if (event.target.closest(".repair-card img")) event.preventDefault();
    };
    document.addEventListener("click", preventCardToggle, true);
    return () => document.removeEventListener("click", preventCardToggle, true);
  }, []);
  const changeLightbox = (step) => setLightbox((current) => { const index = galleryImages.indexOf(current); return galleryImages[(index + step + galleryImages.length) % galleryImages.length]; });
  if (mode !== "curator") return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><p>工具仅在馆长模式下可用。</p><a href={`/game/${encodeURIComponent(gamePath)}`}>返回游戏</a></main>;
  return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><p><a href={`/game/${encodeURIComponent(gamePath)}`}>← 返回游戏</a></p><h1>截图修复工具</h1><p>自动识别屏幕边缘，裁掉屏幕外区域并生成新的修复图片。</p><div className="repair-grid">{candidates.map((image) => <label className="repair-card" key={image}><input type="checkbox" checked={selected.includes(image)} onChange={() => toggle(image)} /><img className="museum-image" src={`/media/${encodeURIComponent(image)}`} onClick={() => setLightbox(image)} /></label>)}</div><p><button disabled={!selected.length || working} onClick={repair}>{working ? "修复中…" : `修复选中的 ${selected.length} 张`}</button></p>{results.length > 0 && <section><h2>本次处理结果</h2><div className="repair-results">{results.map((result) => result.ok ? <div className="repair-result" key={result.output}><img className="museum-image" src={`/media/${encodeURIComponent(result.output)}`} onClick={() => setLightbox(result.output)} /><button title="永久删除" aria-label="永久删除" onClick={() => deleteResult(result.output)}><Trash2 size={18} /></button></div> : <p key={`${result.source}-${result.error}`}>{result.source}：{result.error}</p>)}</div></section>}{fixedImages.length > 0 && <section><h2>已修复截图</h2><div className="repair-grid">{fixedImages.map((image) => <label className="repair-card" key={image}><input type="checkbox" checked={selectedFixed.includes(image)} onChange={() => toggleFixed(image)} /><img className="museum-image" src={`/media/${encodeURIComponent(image)}`} onClick={() => setLightbox(image)} /></label>)}</div><p><button disabled={!selectedFixed.length} onClick={deleteFixed}>永久删除选中的 {selectedFixed.length} 张</button></p></section>}{lightbox && <div className="lightbox" onClick={() => setLightbox(null)}><button onClick={(event) => { event.stopPropagation(); changeLightbox(-1); }}>‹</button><img src={`/media/${encodeURIComponent(lightbox)}`} onClick={(event) => event.stopPropagation()} /><button onClick={(event) => { event.stopPropagation(); changeLightbox(1); }}>›</button></div>}</main>;
}

function GameDetail({ detail, setDetail, mode, setMode, gamePath }) {
  const [duration, setDuration] = useState("300");
  const [lightbox, setLightbox] = useState(null);
  const [draggingImage, setDraggingImage] = useState(null);
  const screenshotImages = preferredScreenshots(detail.images);
  const galleryImages = [...screenshotImages, ...(detail.creative_images || [])];
  const save = async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...data, action: "save" }) }); if (response.ok) setDetail({ ...detail, ...data }); };
  const toggleImage = async (image) => { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "toggle_visibility", image }) }); if (response.ok) { const name = image.split("/").pop(); const hidden = new Set(detail.hidden_images || []); hidden.has(name) ? hidden.delete(name) : hidden.add(name); setDetail({ ...detail, hidden_images: [...hidden] }); } };
  const deleteImage = async (image) => { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete_image", image }) }); if (response.ok) setDetail({ ...detail, images: detail.images.filter((item) => item !== image) }); };
  const archive = async () => { if (confirm("确定归档这个游戏？")) { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "archive" }) }); if (response.ok) window.location.href = "/"; } };
  const remove = async () => { if (confirm("确定删除资料、截图和存档？ROM 不会被删除。")) { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete" }) }); if (response.ok) window.location.href = "/"; } };
  const scrape = async () => { const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "scrape", duration }) }); if (response.ok) { const data = await response.json(); setDetail({ ...detail, images: [...detail.images, ...data.moved] }); } };
  const reorderImage = async (targetImage) => {
    if (!draggingImage || draggingImage === targetImage) return;
    const next = [...screenshotImages];
    const fromIndex = screenshotImages.indexOf(draggingImage);
    const targetIndex = screenshotImages.indexOf(targetImage);
    next.splice(fromIndex, 1);
    next.splice(targetIndex, 0, draggingImage);
    const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "reorder_images", order: next }) });
    if (response.ok) setDetail({ ...detail, images: [...next, ...detail.images.filter((image) => !screenshotImages.includes(image))] });
    setDraggingImage(null);
  };
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
  const renderImage = (image, editable = false, repaired = false, sortable = false) => <div className="image-card" key={image} draggable={sortable} onDragStart={() => setDraggingImage(image)} onDragOver={(event) => event.preventDefault()} onDrop={() => reorderImage(image)}><img className="museum-image" src={`/media/${encodeURIComponent(image)}`} onClick={() => setLightbox(image)} />{repaired && <span className="repair-badge" title="已修复"><Wrench size={16} /></span>}{editable && <span className="image-actions"><button title="切换显示" onClick={() => toggleImage(image)}>{(detail.hidden_images || []).includes(image.split("/").pop()) ? <EyeOff size={16} /> : <Eye size={16} />}</button><button title="移到垃圾桶" onClick={() => deleteImage(image)}><Trash2 size={16} /></button></span>}</div>;
  return <main className={`app-shell ${mode}`}><ModeSwitch mode={mode} setMode={setMode} /><a href="/">← 游戏列表</a>{mode === "curator" && <p><a href={`/game/${encodeURIComponent(gamePath)}/tools`}>工具</a></p>}<h1>{detail.name} <small>{detail.platform}</small></h1>{mode === "curator" && <form onSubmit={save}><p>游戏名<br /><input name="name" defaultValue={detail.name} /></p><p>平台<br /><select name="platform" defaultValue={detail.platform}>{PLATFORMS.map((p) => <option key={p}>{p}</option>)}</select></p><p>存档目录路径（可选）<br /><input name="save_dir" defaultValue={detail.save_dir} /></p><h2>简介</h2><textarea name="description" defaultValue={detail.description} /><h2>随记</h2><textarea name="note" defaultValue={detail.note} /><button>保存</button></form>}<section><h2>简介</h2>{detail.description && <div className="markdown" dangerouslySetInnerHTML={markdown(detail.description)} />}<h2>随记</h2>{detail.note && <div className="markdown" dangerouslySetInnerHTML={markdown(detail.note)} />}</section>{(detail.creative_images || []).length > 0 && <section><h2>二创</h2><div className="image-grid">{detail.creative_images.map((image) => renderImage(image))}</div></section>}<section><h2>截图</h2>{mode === "curator" && <p><select value={duration} onChange={(event) => setDuration(event.target.value)}><option value="300">最近 5 分钟</option><option value="600">最近 10 分钟</option><option value="1800">最近 30 分钟</option><option value="3600">最近 60 分钟</option><option value="18000">最近 5 小时</option></select><button onClick={scrape}>搜刮截图</button></p>}{mode === "curator" && <p className="muted">拖动截图可调整顺序。</p>}<div className="image-grid">{screenshotImages.map((image) => renderImage(image, mode === "curator", mode === "curator" && isFixedImage(image), mode === "curator"))}</div></section>{mode === "curator" && <><NodeEditor detail={detail} setDetail={setDetail} gamePath={gamePath} mode={mode} /><p><button onClick={archive}>归档</button> <button onClick={remove}>删除</button></p></>}{lightbox && <div className="lightbox" onClick={() => setLightbox(null)}><button onClick={(event) => { event.stopPropagation(); changeLightbox(-1); }}>‹</button><img src={`/media/${encodeURIComponent(lightbox)}`} onClick={(event) => event.stopPropagation()} /><button onClick={(event) => { event.stopPropagation(); changeLightbox(1); }}>›</button></div>}</main>;
}

function NodeEditor({ detail, setDetail, gamePath, mode }) {
  if (mode !== "curator") return null;
  const submit = async (event, action, index = -1, form = event.currentTarget) => { event.preventDefault(); const data = Object.fromEntries(new FormData(form)); const response = await fetch(`/api/game/${encodeURIComponent(gamePath)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...data, action, index }) }); if (response.ok) setDetail({ ...detail, nodes: (await response.json()).nodes }); };
  return <section><h2>存档节点</h2>{detail.nodes.map((node, index) => <form className="node-form" key={`${index}-${node.name}`} onSubmit={(event) => submit(event, "node_save", index)}><input name="name" defaultValue={node.name} required /><select name="file" defaultValue={node.file || ""}><option value="">没有关联存档文件</option>{detail.save_files.map((file) => <option key={file}>{file}</option>)}</select><textarea name="description" defaultValue={node.description} /><button>保存节点</button><button type="button" onClick={(event) => submit(event, "node_delete", index, event.currentTarget.form)}>删除节点</button></form>)}<form className="node-form" onSubmit={(event) => submit(event, "node_add")}><input name="name" placeholder="节点名称" required /><select name="file"><option value="">没有关联存档文件</option>{detail.save_files.map((file) => <option key={file}>{file}</option>)}</select><textarea name="description" placeholder="说明" /><button>新增节点</button></form></section>;
}
