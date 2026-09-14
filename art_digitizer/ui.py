"""Small Tk batch workspace backed by the same processor as the CLI."""

from __future__ import annotations

import json
import mimetypes
import threading
import tkinter as tk
import uuid
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import unquote

from .models import ImageItem, PipelineConfig, Project, config_from_preset
from .models import resolve_output_dir
from .processor import process_batch

STAGES = ["Original", "Background Removed", "Threshold", "Cleaned Mask", "Contours", "Polygon", "3D Mesh", "Final Preview"]

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES, TkinterDnD = None, None

WEB_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Art Digitizer | Batch</title>
<style>body{font:15px system-ui;margin:0;background:#f3f0e8;color:#24221e}main{max-width:1000px;margin:32px auto;padding:0 20px}h1{font-size:32px}.bar,.settings{background:#fff;border:1px solid #d7d0c2;padding:16px;margin:14px 0}input,button{padding:9px;margin:4px}button{background:#1f5c52;color:#fff;border:0;cursor:pointer}.item{display:flex;justify-content:space-between;border-bottom:1px solid #eee;padding:10px;cursor:pointer}.status{color:#1f5c52}.live{background:#1f5c52;color:#fff;padding:12px;margin-top:12px}.grid{display:grid;grid-template-columns:2fr 1fr;gap:14px}.preview{width:100%;min-height:260px;object-fit:contain;background:#eee;margin-top:12px}.outputs a{display:block;color:#1f5c52;margin:5px 0}@media(max-width:700px){.grid{grid-template-columns:1fr}}</style></head>
<body><main><h1>Art Digitizer / Batch</h1><div class="bar"><input id="files" type="file" accept=".png,.jpg,.jpeg,.tif,.tiff" multiple><input id="folder" type="file" webkitdirectory multiple><button onclick="runBatch()">Process All</button><div class="live" id="live">Live pipeline idle</div></div>
<div class="grid"><section class="bar"><h2>Batch</h2><div id="items">No images selected.</div></section><section class="settings"><h2>Pipeline Settings</h2><label>Threshold <input id="threshold" type="number" min="0" max="255" value="142"></label><br><label>Minimum contour size <input id="minimum" type="number" min="0" value="15"></label><br><label><input id="auto" type="checkbox"> Auto detect per image</label><p id="modeNote">Fixed mode applies these exact values to every image.</p><h2>Live Preview</h2><div id="previewLabel">Select an item while processing.</div><img id="preview" class="preview" alt="Processing preview"><div id="outputs" class="outputs"></div></section></div></main>
<script>const input=document.getElementById('files'),folder=document.getElementById('folder'),items=document.getElementById('items'),live=document.getElementById('live'),preview=document.getElementById('preview'),previewLabel=document.getElementById('previewLabel'),outputs=document.getElementById('outputs'),auto=document.getElementById('auto'),modeNote=document.getElementById('modeNote');let files=[],currentJob='';const stageFiles={'Background Removed':'background_removed.png','Threshold':'threshold.png','Cleaned Mask':'cleaned_mask.png','Contours':'contours.png','Polygon':'polygon.png','3D Mesh':'mesh.png','Final Preview':'final_preview.png','Complete':'final_preview.png','Completed (cached)':'final_preview.png'};auto.onchange=()=>modeNote.textContent=auto.checked?'Auto mode chooses deterministic threshold and contour size per image; values are recorded in the manifest.':'Fixed mode applies these exact values to every image.';function add(list){files=[...files,...list].filter((f,i,a)=>a.findIndex(x=>x.name===f.name&&x.size===f.size)===i);render()}function render(){items.innerHTML=files.length?files.map((f,i)=>`<div class="item" onclick="showPreview(${i},'Original')"><span>${f.name}</span><span class="status" id="s${i}">Ready</span></div>`).join(''):'No images selected.'}function showPreview(index,stage){if(!currentJob)return;const filename=stageFiles[stage]||'original.png';preview.src='/api/jobs/'+currentJob+'/assets/'+index+'/'+filename+'?t='+Date.now();previewLabel.textContent=files[index]?files[index].name+' / '+stage:stage;}function showOutputs(index,asset){if(!asset.outputs)return;outputs.innerHTML=asset.outputs.map(name=>`<a href="/api/jobs/${currentJob}/assets/${index}/${name}" target="_blank">Download ${name}</a>`).join('')}input.onchange=e=>add([...e.target.files]);folder.onchange=e=>add([...e.target.files]);async function poll(job){const response=await fetch('/api/jobs/'+job);const result=await response.json();result.assets.forEach((a,i)=>{const node=document.getElementById('s'+i);if(node)node.textContent=a.status+(a.stage?' / '+a.stage:'')+(a.error?': '+a.error:'');if(a.stage)showPreview(i,a.stage);if(a.status==='Completed')showOutputs(i,a)});if(result.current){live.textContent='Live: '+result.current.filename+' / '+result.current.stage;const index=result.assets.findIndex(a=>a.source_filename===result.current.filename);if(index>=0)showPreview(index,result.current.stage)}if(result.status==='Queued'||result.status==='Processing'){setTimeout(()=>poll(job),250)}else{live.textContent='Batch '+result.status.toLowerCase();result.assets.forEach((a,i)=>showOutputs(i,a));}}async function runBatch(){if(!files.length)return;const data=new FormData();files.forEach(f=>data.append('files',f,f.name));data.append('threshold',threshold.value);data.append('minimum',minimum.value);data.append('mode',auto.checked?'per_image_automatic':'global_fixed');files.forEach((_,i)=>document.getElementById('s'+i).textContent='Queued');live.textContent='Uploading batch...';try{const response=await fetch('/api/process',{method:'POST',body:data});const result=await response.json();if(!response.ok)throw new Error(result.error||'Batch request failed');currentJob=result.job_id;poll(currentJob);}catch(error){files.forEach((_,i)=>document.getElementById('s'+i).textContent='Failed: '+error.message);live.textContent='Batch failed';}}</script></body></html>"""


def web_main():
    jobs = {}
    jobs_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                payload = WEB_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path.startswith("/api/jobs/"):
                parts = [unquote(part) for part in self.path.split("/")]
                job_id = parts[3] if len(parts) > 3 else ""
                with jobs_lock:
                    job = jobs.get(job_id)
                    snapshot = json.loads(json.dumps(job)) if job else None
                if snapshot is None:
                    self.send_error(404)
                elif len(parts) >= 7 and parts[4] == "assets":
                    try:
                        index = int(parts[5])
                        filename = Path(parts[6]).name
                        source = Path(snapshot["assets"][index]["source_filename"]).name
                        base = (Path("web_exports") / "processed").resolve()
                        target = (resolve_output_dir(base, source) / filename).resolve()
                        if not target.is_relative_to(base) or not target.is_file():
                            raise FileNotFoundError(filename)
                        payload = target.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", mimetypes.guess_type(filename)[0] or "application/octet-stream")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    except (IndexError, ValueError, FileNotFoundError):
                        self.send_error(404)
                else:
                    self.send_json(200, snapshot)
                return
            self.send_error(404)

        def do_POST(self):
            if self.path != "/api/process":
                self.send_error(404)
                return
            try:
                content_type = self.headers.get("Content-Type", "")
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                message = BytesParser(policy=default).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
                )
                root = Path("web_exports")
                input_root = root / "uploads"
                input_root.mkdir(parents=True, exist_ok=True)
                images = []
                fields = {}
                for part in message.walk():
                    if part.is_multipart():
                        continue
                    filename = Path(part.get_filename() or "upload.png").name
                    if part.get_filename():
                        if Path(filename).suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
                            continue
                        target = input_root / filename
                        target.write_bytes(part.get_payload(decode=True) or b"")
                        images.append(ImageItem(str(target), len(images)))
                    else:
                        name = part.get_param("name", header="content-disposition")
                        fields[name] = (part.get_payload(decode=True) or b"").decode().strip()
                config = PipelineConfig(threshold=int(fields.get("threshold", 142)), minimum_contour_size=int(fields.get("minimum", 15)))
                if not images:
                    raise ValueError("No supported images were uploaded")
                project = Project(global_config=config, images=images, processing_mode=fields.get("mode", "global_fixed"))
                job_id = uuid.uuid4().hex
                with jobs_lock:
                    jobs[job_id] = {
                        "status": "Queued",
                        "assets": [{"source_filename": image.source, "status": "Queued", "stage": "Queued"} for image in images],
                        "current": None,
                    }

                def run_job():
                    def on_stage(item, stage):
                        with jobs_lock:
                            jobs[job_id]["status"] = "Processing"
                            jobs[job_id]["current"] = {"filename": Path(item.source).name, "stage": stage}
                            jobs[job_id]["assets"][item.order].update({"status": "Processing", "stage": stage})

                    def on_update(item, result):
                        with jobs_lock:
                            jobs[job_id]["assets"][item.order] = {
                                "source_filename": Path(item.source).name,
                                "status": result.get("status", item.status),
                                "stage": "Complete",
                                "error": result.get("error"),
                                "warnings": result.get("warnings", []),
                            }

                    try:
                        result = process_batch(project, root / "processed", on_update=on_update, on_stage=on_stage)
                        with jobs_lock:
                            jobs[job_id].update(result)
                            for index, asset in enumerate(result.get("assets", [])):
                                jobs[job_id]["assets"][index]["stage"] = "Complete"
                            jobs[job_id]["status"] = "Completed" if not result["summary"]["failed"] else "Completed with failures"
                    except Exception as error:
                        with jobs_lock:
                            jobs[job_id]["status"] = "Failed"
                            jobs[job_id]["error"] = str(error)

                threading.Thread(target=run_job, daemon=True).start()
                self.send_json(202, {"job_id": job_id, "status": "Queued"})
            except Exception as error:
                self.send_json(400, {"assets": [], "error": str(error)})

        def send_json(self, status, value):
            payload = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    url = "http://127.0.0.1:8000"
    print(f"Batch web UI: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()


class BatchWorkspace:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Art Digitizer | Batch")
        self.root.geometry("1050x650")
        self.project = Project()
        self.output_dir = Path("processed")
        self.preview_image = None
        self.items: list[ImageItem] = []
        self.status = tk.StringVar(value="Ready")
        self.threshold = tk.IntVar(value=142)
        self.minimum_size = tk.IntVar(value=15)
        self.override_threshold = tk.IntVar(value=142)
        self.mode = tk.StringVar(value="global_fixed")
        self.stage = tk.StringVar(value=STAGES[0])
        self._build()
        if DND_FILES:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self.drop_files)

    def _build(self):
        toolbar = ttk.Frame(self.root, padding=10)
        toolbar.pack(fill="x")
        for label, command in (("Add Images", self.add_images), ("Add Folder", self.add_folder), ("Remove", self.remove), ("Move Up", self.move_up), ("Move Down", self.move_down), ("Open Project", self.open_project), ("Save Project", self.save_project)):
            ttk.Button(toolbar, text=label, command=command).pack(side="left", padx=3)
        ttk.Label(toolbar, textvariable=self.status).pack(side="right")

        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        left = ttk.Frame(body, padding=8)
        right = ttk.Frame(body, padding=8)
        body.add(left, weight=3)
        body.add(right, weight=2)

        ttk.Label(left, text="BATCH", font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        self.listbox = tk.Listbox(left, selectmode="extended", height=20, exportselection=False)
        self.listbox.pack(fill="both", expand=True, pady=8)
        self.listbox.bind("<<ListboxSelect>>", self.select_item)
        actions = ttk.Frame(left)
        actions.pack(fill="x")
        ttk.Button(actions, text="Process Selected", command=lambda: self.start_processing(False)).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="Process All", command=lambda: self.start_processing(True)).pack(side="left")
        ttk.Button(actions, text="Reprocess Batch", command=lambda: self.start_processing(True, force=True)).pack(side="right")

        ttk.Label(right, text="GLOBAL FIXED SETTINGS", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        settings = ttk.Frame(right)
        settings.pack(fill="x", pady=8)
        ttk.Label(settings, text="Threshold").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Spinbox(settings, from_=0, to=255, textvariable=self.threshold, command=self.update_config).grid(row=0, column=1, sticky="ew")
        ttk.Label(settings, text="Minimum contour size").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(settings, from_=0, to=100000, textvariable=self.minimum_size, command=self.update_config).grid(row=1, column=1, sticky="ew")
        ttk.Label(settings, text="Processing mode").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(settings, textvariable=self.mode, values=["global_fixed", "per_image_automatic"], state="readonly").grid(row=2, column=1, sticky="ew")
        settings.columnconfigure(1, weight=1)
        ttk.Separator(right).pack(fill="x", pady=12)
        ttk.Label(right, text="SELECTED IMAGE", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        self.selected_label = ttk.Label(right, text="No image selected")
        self.selected_label.pack(anchor="w", pady=6)
        ttk.Button(right, text="Reset to Global Settings", command=self.reset_override).pack(anchor="w")
        override_row = ttk.Frame(right)
        override_row.pack(fill="x", pady=6)
        ttk.Spinbox(override_row, from_=0, to=255, textvariable=self.override_threshold, width=7).pack(side="left")
        ttk.Button(override_row, text="Set Threshold Override", command=self.set_override).pack(side="left", padx=5)
        ttk.Label(right, text="Stage preview").pack(anchor="w", pady=(18, 4))
        stage_picker = ttk.Combobox(right, textvariable=self.stage, values=STAGES, state="readonly")
        stage_picker.pack(fill="x")
        stage_picker.bind("<<ComboboxSelected>>", lambda _event: self.show_preview())
        self.preview = ttk.Label(right, text="Select an image and process it to inspect its stages.", anchor="center", relief="sunken")
        self.preview.pack(fill="both", expand=True, pady=8)

    def update_config(self):
        self.project.global_config.threshold = self.threshold.get()
        self.project.global_config.minimum_contour_size = self.minimum_size.get()

    def add_images(self):
        paths = filedialog.askopenfilenames(filetypes=[("Images", "*.png *.jpg *.jpeg *.tif *.tiff")])
        self.add_paths(paths)

    def add_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.add_paths(sorted(str(item) for item in Path(path).rglob("*") if item.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}))

    def drop_files(self, event):
        self.add_paths(self.root.tk.splitlist(event.data))

    def add_paths(self, paths):
        existing = {Path(item.source).resolve() for item in self.items}
        for path in paths:
            if Path(path).resolve() not in existing:
                self.items.append(ImageItem(source=str(path), order=len(self.items)))
        self.refresh()

    def refresh(self):
        self.listbox.delete(0, "end")
        for item in self.items:
            marker = " OVERRIDE" if item.override else ""
            self.listbox.insert("end", f"{Path(item.source).name}    {item.status}{marker}")

    def selected_index(self):
        selection = self.listbox.curselection()
        return selection[0] if selection else None

    def selected_item(self):
        index = self.selected_index()
        return self.items[index] if index is not None else None

    def select_item(self, _event=None):
        item = self.selected_item()
        if item:
            self.selected_label.configure(text=f"{Path(item.source).name} | {item.status}" + (" | OVERRIDE" if item.override else ""))
            self.override_threshold.set(item.override.get("threshold", self.threshold.get()))
            self.show_preview()

    def show_preview(self):
        item = self.selected_item()
        if not item or not item.outputs:
            self.preview.configure(image="", text="Select an image and process it to inspect its stages.")
            return
        stage_filename = {
            "Original": "original.png", "Background Removed": "background_removed.png", "Threshold": "threshold.png",
            "Cleaned Mask": "cleaned_mask.png", "Contours": "contours.png", "Polygon": "polygon.png",
            "3D Mesh": "final_preview.png", "Final Preview": "final_preview.png",
        }[self.stage.get()]
        path = self.output_dir / Path(item.source).stem / stage_filename
        if not path.exists():
            return
        try:
            from PIL import Image, ImageTk
            image = Image.open(path).copy()
            image.thumbnail((430, 360))
            self.preview_image = ImageTk.PhotoImage(image)
            self.preview.configure(image=self.preview_image, text="")
        except Exception:
            self.preview.configure(image="", text=str(path))

    def remove(self):
        for index in reversed(self.listbox.curselection()):
            self.items.pop(index)
        for index, item in enumerate(self.items):
            item.order = index
        self.refresh()

    def move_up(self):
        index = self.selected_index()
        if index and index > 0:
            self.items[index - 1], self.items[index] = self.items[index], self.items[index - 1]
            self.items[index - 1].order, self.items[index].order = index - 1, index
            self.refresh()
            self.listbox.selection_set(index - 1)

    def move_down(self):
        index = self.selected_index()
        if index is not None and index < len(self.items) - 1:
            self.items[index + 1], self.items[index] = self.items[index], self.items[index + 1]
            self.items[index].order, self.items[index + 1].order = index, index + 1
            self.refresh()
            self.listbox.selection_set(index + 1)

    def reset_override(self):
        item = self.selected_item()
        if item:
            item.override = {}
            self.override_threshold.set(self.threshold.get())
            self.refresh()

    def set_override(self):
        item = self.selected_item()
        if item:
            item.override["threshold"] = self.override_threshold.get()
            self.refresh()
            self.select_item()

    def start_processing(self, all_items: bool, force: bool = False):
        self.update_config()
        selected = set(self.listbox.curselection())
        items = self.items if all_items else [item for index, item in enumerate(self.items) if index in selected]
        if not items:
            messagebox.showinfo("Batch", "Add or select at least one image.")
            return
        output = filedialog.askdirectory(title="Choose output folder")
        if not output:
            return
        self.project.images = items
        self.project.processing_mode = self.mode.get()
        self.status.set("Processing...")
        threading.Thread(target=self._process, args=(output, force), daemon=True).start()

    def _process(self, output, force):
        try:
            self.output_dir = Path(output)
            process_batch(self.project, output, force=force, on_update=lambda _item, _result: self.root.after(0, self.refresh))
            self.root.after(0, lambda: self.status.set("Batch complete"))
        except Exception as error:
            self.root.after(0, lambda: messagebox.showerror("Batch failed", str(error)))
            self.root.after(0, lambda: self.status.set("Failed"))

    def save_project(self):
        self.update_config()
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Project JSON", "*.json")])
        if path:
            project = Project(name=Path(path).stem, global_config=self.project.global_config, preset_name=self.project.preset_name, images=self.items, processing_mode=self.mode.get())
            Path(path).write_text(json.dumps(project.to_dict(), indent=2) + "\n", encoding="utf-8")

    def open_project(self):
        path = filedialog.askopenfilename(filetypes=[("Project JSON", "*.json")])
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.project = Project.from_dict(data)
            self.items = self.project.images
            self.threshold.set(self.project.global_config.threshold)
            self.minimum_size.set(self.project.global_config.minimum_contour_size)
            self.mode.set(self.project.processing_mode)
            self.refresh()


def main():
    try:
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    except tk.TclError as error:
        if "display" in str(error).lower():
            web_main()
            return
        raise
    BatchWorkspace(root)
    root.mainloop()


if __name__ == "__main__":
    main()
