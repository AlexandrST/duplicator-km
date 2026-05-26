import os
import re
import subprocess
import tempfile
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from PIL import Image, ImageTk


GS = "\x1d"  # ASCII 29 (Group Separator)


@dataclass(frozen=True)
class NormalizeResult:
    raw: str
    normalized_gs: str  # contains real GS char
    normalized_view: str  # shows <GS>
    zint_gs1: str  # GS1 AI format for zint: [01]...[21]...[93]...


def normalize_km(raw: str) -> NormalizeResult:
    s = (raw or "").strip().replace("\r", "").replace("\n", "").replace("\t", "").replace(" ", "")
    # AIM prefixes sometimes present
    if s.startswith("]d2") or s.startswith("]C1"):
        s = s[3:]

    # If already contains GS, keep it (but still normalize view)
    if GS in s:
        # Convert GS-separated GS1 string into Zint AI format (no control chars).
        # Typical:
        # - 01<GTIN14>21<SN> <GS> 93<CHECK4>
        # - 01<GTIN14>21<SN> <GS> 91<...> <GS> 92<...>

        def zint_ai_from_gs_payload(payload: str) -> str | None:
            parts = payload.split(GS)
            if not parts or not parts[0].startswith("01"):
                return None

            p0 = parts[0]
            # p0 must at least contain 01 + GTIN14 + 21 + something
            if len(p0) < 2 + 14 + 2:
                return None
            if p0[0:2] != "01" or not p0[2:16].isdigit() or p0[16:18] != "21":
                return None

            gtin = p0[2:16]
            serial = p0[18:]
            if serial == "":
                return None

            out = [f"[01]{gtin}", f"[21]{serial}"]

            for seg in parts[1:]:
                if not seg:
                    continue
                # Most common in марки: 91/92/93 are variable-length (until GS / end)
                ai = seg[:2]
                data = seg[2:]
                if len(ai) != 2 or not ai.isdigit() or data == "":
                    return None
                out.append(f"[{ai}]{data}")

            return "".join(out)

        zint = zint_ai_from_gs_payload(s)
        if not zint:
            # Last-resort fallback: strip GS (some inputs may already be in AI bracket form)
            zint = s.replace(GS, "")

        return NormalizeResult(raw=raw, normalized_gs=s, normalized_view=s.replace(GS, "<GS>"), zint_gs1=zint)

    # Pattern: 01 GTIN14 21 SN(6) 93 CHECK(4)
    m = re.fullmatch(r"(01)(\d{14})(21)(.{6})(93)(.{4})", s)
    if m:
        normalized = f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{GS}{m.group(5)}{m.group(6)}"
        zint = f"[01]{m.group(2)}[21]{m.group(4)}[93]{m.group(6)}"
        return NormalizeResult(
            raw=raw,
            normalized_gs=normalized,
            normalized_view=normalized.replace(GS, "<GS>"),
            zint_gs1=zint,
        )

    # Fallback: return as-is (no GS inserted)
    return NormalizeResult(raw=raw, normalized_gs=s, normalized_view=s.replace(GS, "<GS>"), zint_gs1=s)


def require_zint() -> str:
    exe = "zint.exe" if os.name == "nt" else "zint"
    candidates: list[str] = []

    # 1) explicit env var
    env_path = os.environ.get("ZINT_PATH")
    if env_path:
        candidates.append(env_path)

    # 2) local рядом с app.py
    local_path = os.path.join(os.path.dirname(__file__), exe)
    candidates.append(local_path)

    # 3) PATH
    candidates.append(exe)

    for cand in candidates:
        try:
            subprocess.run([cand, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return cand
        except Exception:  # noqa: BLE001
            continue

    raise RuntimeError(
        "Не найден zint. Варианты:\n"
        "- установите Zint и добавьте zint(.exe) в PATH\n"
        "- или положите zint.exe рядом с app.py\n"
        "- или задайте переменную окружения ZINT_PATH (полный путь к zint.exe)"
    )


def render_gs1_datamatrix_png(data_with_gs: str, size_px: int = 480) -> Image.Image:
    exe = require_zint()
    # Zint GS1 mode expects AI parentheses or raw with FNC1/GS handling. With --gs1 and DM it will encode as GS1.
    # For DM symbology: --barcode=71 (Data Matrix)
    # Use --square to keep it square-ish, and set --scale to reach approximate size.
    # We write to temp file.
    with tempfile.TemporaryDirectory() as td:
        out_png = os.path.join(td, "dm.png")

        # Heuristic scale: 4 is usually fine; we will resize in Pillow.
        # Important: pass data via -d to avoid argument parsing issues on Windows
        cmd = [
            exe,
            "--barcode=71",
            "--gs1",
            "--notext",
            "--scale=4",
            "--output",
            out_png,
            "-d",
            data_with_gs,
        ]
        p = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode != 0:
            raise RuntimeError(
                f"zint returncode={p.returncode}\n\n"
                f"DATA:\n{data_with_gs}\n\n"
                f"STDERR:\n{p.stderr}\n\n"
                f"CMD:\n{cmd}"
            )
        img = Image.open(out_png).convert("RGBA")
        img = img.resize((size_px, size_px), resample=Image.NEAREST)
        return img


def mm_to_px(mm: float, dpi: int) -> int:
    return max(1, int(round(mm / 25.4 * dpi)))


def compose_with_border(code_img: Image.Image, code_mm: float, border_mm: float, dpi: int) -> Image.Image:
    code_px = mm_to_px(code_mm, dpi)
    border_px = mm_to_px(border_mm, dpi)
    total_px = code_px + border_px * 2
    code_resized = code_img.resize((code_px, code_px), resample=Image.NEAREST)
    canvas = Image.new("RGBA", (total_px, total_px), (255, 255, 255, 255))
    canvas.paste(code_resized, (border_px, border_px))
    return canvas


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Дубликатор марок (GS1 DataMatrix)")
        self.geometry("820x680")

        self.raw_var = tk.StringVar()
        self.norm_var = tk.StringVar()
        self.status_var = tk.StringVar(value="")
        self.preview_px_var = tk.IntVar(value=280)
        self.code_mm_var = tk.DoubleVar(value=12.0)
        self.border_mm_var = tk.DoubleVar(value=4.5)
        self.dpi_var = tk.IntVar(value=300)
        self._preview_imgtk: ImageTk.PhotoImage | None = None
        self._print_img: Image.Image | None = None
        self._last_norm: NormalizeResult | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 8}

        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Отсканированная марка (строка КМ):").pack(anchor="w", **pad)
        txt = tk.Text(frm, height=4, wrap="word")
        txt.pack(fill="x", **pad)

        menu = tk.Menu(txt, tearoff=False)
        menu.add_command(label="Копировать", command=lambda: txt.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: txt.event_generate("<<Paste>>"))
        menu.add_command(label="Вырезать", command=lambda: txt.event_generate("<<Cut>>"))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: txt.event_generate("<<SelectAll>>"))

        def show_menu(e: tk.Event) -> str:
            txt.focus_set()
            menu.tk_popup(e.x_root, e.y_root)
            return "break"

        # Явно добавляем copy/paste и контекстное меню, т.к. на некоторых системах
        # стандартные биндинги Tk могут быть недоступны/переопределены.
        txt.bind("<Button-3>", show_menu)
        txt.bind("<Control-c>", lambda _e: (txt.event_generate("<<Copy>>"), "break")[1])
        txt.bind("<Control-v>", lambda _e: (txt.event_generate("<<Paste>>"), "break")[1])
        txt.bind("<Control-x>", lambda _e: (txt.event_generate("<<Cut>>"), "break")[1])
        txt.bind("<Control-a>", lambda _e: (txt.event_generate("<<SelectAll>>"), "break")[1])

        def sync_text_to_var(*_args: object) -> None:
            self.raw_var.set(txt.get("1.0", "end").strip())

        txt.bind("<<Modified>>", lambda e: (txt.edit_modified(False), sync_text_to_var()))

        btn_row = ttk.Frame(frm)
        btn_row.pack(fill="x", **pad)

        ttk.Button(btn_row, text="Построить", command=self.on_build).pack(side="left")
        ttk.Button(btn_row, text="Очистить", command=lambda: (txt.delete("1.0", "end"), self.on_clear())).pack(
            side="left", padx=8
        )
        ttk.Button(btn_row, text="Сохранить PNG…", command=self.on_save_png).pack(side="left", padx=8)
        ttk.Button(btn_row, text="Печать…", command=self.on_print).pack(side="left", padx=8)
        ttk.Label(btn_row, text="Предпросмотр (px):").pack(side="left", padx=(16, 6))
        ttk.Spinbox(btn_row, from_=140, to=800, increment=20, textvariable=self.preview_px_var, width=6).pack(
            side="left"
        )

        cfg_row = ttk.Frame(frm)
        cfg_row.pack(fill="x", **pad)
        ttk.Label(cfg_row, text="Код (мм):").pack(side="left")
        ttk.Spinbox(cfg_row, from_=6.0, to=40.0, increment=0.5, textvariable=self.code_mm_var, width=6).pack(
            side="left", padx=(6, 16)
        )
        ttk.Label(cfg_row, text="Рамка (мм):").pack(side="left")
        ttk.Spinbox(cfg_row, from_=0.0, to=20.0, increment=0.5, textvariable=self.border_mm_var, width=6).pack(
            side="left", padx=(6, 16)
        )
        ttk.Label(cfg_row, text="DPI:").pack(side="left")
        ttk.Spinbox(cfg_row, from_=150, to=600, increment=50, textvariable=self.dpi_var, width=6).pack(side="left")

        ttk.Label(frm, text="Нормализованный код (для контроля):").pack(anchor="w", **pad)
        norm_entry = ttk.Entry(frm, textvariable=self.norm_var, state="readonly")
        norm_entry.pack(fill="x", **pad)

        ttk.Separator(frm).pack(fill="x", **pad)

        ttk.Label(frm, text="Предпросмотр:").pack(anchor="w", **pad)
        self.preview = ttk.Label(frm)
        self.preview.pack(fill="both", expand=True, **pad)

        self.status = ttk.Label(frm, textvariable=self.status_var, foreground="#555")
        self.status.pack(anchor="w", **pad)

    def on_clear(self) -> None:
        self.norm_var.set("")
        self.status_var.set("")
        self._preview_imgtk = None
        self._print_img = None
        self._last_norm = None
        self.preview.configure(image="")

    def on_build(self) -> None:
        raw = self.raw_var.get()
        if not raw.strip():
            messagebox.showwarning("Дубликатор марок", "Введите или отсканируйте строку КМ.")
            return

        try:
            norm = normalize_km(raw)
            self._last_norm = norm
            self.norm_var.set(norm.normalized_view)
            code = render_gs1_datamatrix_png(norm.zint_gs1, size_px=600)
            full_img = compose_with_border(
                code_img=code,
                code_mm=float(self.code_mm_var.get()),
                border_mm=float(self.border_mm_var.get()),
                dpi=int(self.dpi_var.get()),
            )
            self._print_img = full_img
            preview_px = int(self.preview_px_var.get())
            preview_img = full_img.resize((preview_px, preview_px), resample=Image.NEAREST)
            self._preview_imgtk = ImageTk.PhotoImage(preview_img)
            self.preview.configure(image=self._preview_imgtk)
            self.status_var.set("Готово. Для корректного GS1 нужен <GS> перед AI 93.")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Ошибка", str(e))

    def on_save_png(self) -> None:
        if not self._last_norm:
            messagebox.showwarning("Дубликатор марок", "Сначала нажмите «Построить».")
            return
        try:
            code = render_gs1_datamatrix_png(self._last_norm.zint_gs1, size_px=1200)
            img = compose_with_border(
                code_img=code,
                code_mm=float(self.code_mm_var.get()),
                border_mm=float(self.border_mm_var.get()),
                dpi=int(self.dpi_var.get()),
            )
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Ошибка", str(e))
            return

        # Minimal: save to temp; real file dialog omitted for simplicity
        out = os.path.join(os.getcwd(), "datamatrix.png")
        img.save(out, "PNG")
        messagebox.showinfo("Дубликатор марок", f"Сохранено: {out}")

    def on_print(self) -> None:
        if not self._print_img:
            messagebox.showwarning("Дубликатор марок", "Сначала нажмите «Построить».")
            return

        try:
            fd, tmp_path = tempfile.mkstemp(prefix="datamatrix_", suffix=".png")
            os.close(fd)
            self._print_img.save(tmp_path, "PNG")

            if os.name == "nt":
                os.startfile(tmp_path, "print")  # noqa: S606
            else:
                # Prefer CUPS lp, fallback to lpr
                try:
                    subprocess.run(["lp", tmp_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:  # noqa: BLE001
                    subprocess.run(["lpr", tmp_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            self.status_var.set("Отправлено на печать.")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Ошибка печати", str(e))


if __name__ == "__main__":
    App().mainloop()

