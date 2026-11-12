"""CATIA V5 adapter (COM).

Sketch + Pad for the base solid, then sketch + Pocket on a plane offset to the
top face for every feature.  Verified against analytic volume.
"""
from __future__ import annotations

import math
import math as _math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from ..calc import feature_depth, part_volume
from ..geom import flatten, rect_outline, slot_outline
from ..models import PartSpec
from .base import BuildResult
from .guard import acquire as _cad_acquire, release as _cad_release
from .memguard import check as _mem_check


_PID_CACHE = {"t": 0.0, "pids": set()}


def _catia_pids(ttl: float = 8.0, force: bool = False):
    """CATIA 自身的 PID 集合。覆盖确认守卫绝不能去点别的程序的框。

    CNEXT.exe 是 CATIA V5 的实际进程名；CATStart 只是启动器。
    取不到就返回空集 —— 空集意味着「一个都不点」，宁可让导出失败报错，
    也不能误点别人（用户正在编辑的文档、别的软件）的「是」。

    结果缓存 ttl 秒：守卫每 0.3s 扫一次，每次都起一次 tasklist 太重。
    """
    now = time.time()
    if not force and _PID_CACHE["pids"] and now - _PID_CACHE["t"] < ttl:
        return set(_PID_CACHE["pids"])
    names = {"CNEXT.exe", "CNEXT", "CATIA.exe", "CATIA"}
    pids = set()
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             errors="replace").stdout
        for line in out.splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) < 2:
                continue
            pname, spid = parts[0].strip('"'), parts[1].strip('"')
            if pname in names:
                try:
                    pids.add(int(spid))
                except ValueError:
                    pass
    except Exception:
        pass
    if pids:
        _PID_CACHE["t"] = now
        _PID_CACHE["pids"] = set(pids)
    return pids


def _start_overwrite_guard(log: Callable[[str], None]):
    """CATIA 另存/导出遇到同名文件会弹模态「要覆盖它吗？」。

    这个框会卡住 COM 调用并报「方法 SaveAs 失败」。后台起一个线程，
    只针对真正的「已经存在…要覆盖它吗」确认框点「是」，避免误点
    正常对话框而打断导出。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindows = user32.EnumWindows
    EnumChildWindows = user32.EnumChildWindows
    GetWindowTextW = user32.GetWindowTextW
    GetClassNameW = user32.GetClassNameW
    PostMessageW = user32.PostMessageW
    IsWindowVisible = user32.IsWindowVisible
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    stop = {"go": False}
    markers = ("已经存在", "已存在", "要覆盖", "overwrite", "Overwrite", "replace it")
    # 「已存在于会话中。请尝试另一名称。」这个框没有「是」，只能点「确定」退出，
    # 否则 SaveAs 会一直卡住。它是不可恢复的，但至少要让调用尽快失败返回。
    dup_markers = ("已存在于会话中", "请尝试另一名称", "already exists in the session")

    def _text_of(h):
        b = ctypes.create_unicode_buffer(512)
        GetWindowTextW(h, b, 512)
        return b.value

    def _class_of(h):
        b = ctypes.create_unicode_buffer(128)
        GetClassNameW(h, b, 128)
        return b.value

    def _children(hwnd):
        out = []
        cb = WNDENUMPROC(lambda h, l: (out.append(h), True)[1])
        EnumChildWindows(hwnd, cb, 0)
        return out

    def _candidates():
        hits = []
        skipped = []
        owners = _catia_pids()

        def _pid(hwnd):
            p = wintypes.DWORD()
            try:
                GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            except Exception:
                return 0
            return p.value

        def cb(hwnd, lparam):
            if IsWindowVisible(hwnd):
                t = _text_of(hwnd)
                if t in ("另存为", "Save As", "Export", "导出", "警告", "Warning"):
                    body = " ".join(_text_of(c) for c in _children(hwnd))
                    if not (any(mk in body for mk in markers) or
                            any(mk in body for mk in dup_markers)):
                        return True
                    # 关键：只认 CATIA 自己进程的框。别的程序（含用户正在
                    # 编辑的文档）弹同样的窗，一律不碰。
                    owner = _pid(hwnd)
                    if owner not in owners:
                        # 缓存没命中时强制刷新一次：CATIA 若重启过，pid 变了，
                        # 光靠缓存会把它自己的框当成"外来程序"而放过（导出会失败）。
                        owners_now = _catia_pids(force=True)
                        if owner not in owners_now:
                            skipped.append((owner, t, body[:60]))
                            return True
                    if any(mk in body for mk in markers):
                        hits.append((hwnd, "yes"))
                    else:
                        hits.append((hwnd, "ok"))
            return True

        EnumWindows(WNDENUMPROC(cb), 0)
        return hits, skipped

    def _click_yes(dlg):
        clicked = False
        for h in _children(dlg):
            if _class_of(h) == "Button" and _text_of(h)[:1] in ("是", "&Y", "Y"):
                PostMessageW(h, 0x00F5, None, None)   # BM_CLICK
                clicked = True
        return clicked

    def _click_ok(dlg):
        for h in _children(dlg):
            if _class_of(h) == "Button" and _text_of(h)[:2] in ("确定", "OK"):
                PostMessageW(h, 0x00F5, None, None)   # BM_CLICK
                return True
        return False

    def loop():
        # 同一个窗口只点一次。模态框点完到真正消失之间有几百毫秒，
        # 期间若重复点击，用户手动点了「否」也会被立刻改回「是」。
        clicked_hwnds = set()
        told = set()
        while not stop["go"]:
            hits, skipped = _candidates()
            alive = {dlg for dlg, _ in hits}
            clicked_hwnds &= alive          # 窗口消失后才允许再次处理同 hwnd
            for owner, title, body in skipped:
                key = (owner, title)
                if key not in told:
                    told.add(key)
                    log(f"CATIA 覆盖守卫：忽略非 CATIA 进程（pid={owner}）的"
                        f"「{title}」窗口，未点击")
            for dlg, kind in hits:
                if dlg in clicked_hwnds:
                    continue
                if kind == "yes" and _click_yes(dlg):
                    clicked_hwnds.add(dlg)
                    log("CATIA 覆盖确认框已自动确认")
                elif kind == "ok" and _click_ok(dlg):
                    clicked_hwnds.add(dlg)
                    log("CATIA 报「文档已存在于会话中」，已点确定释放 COM（本次保存会失败）")
            time.sleep(0.3)

    import threading
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return stop


def _stop_overwrite_guard(stop) -> None:
    if stop is not None:
        stop["go"] = True


class CatiaAdapter:
    name = "CATIA"

    def __init__(self, exe: str = "", env_dir: str = "", env_name: str = ""):
        self.exe = exe
        self.env_dir = env_dir
        self.env_name = env_name

    SAFE = re.compile(r"[^0-9A-Za-z_\-]+")

    def _safe_name(self, name: str) -> str:
        s = self.SAFE.sub("_", name).strip("_")
        if len(s) < 3 or any(ord(ch) > 127 for ch in name):
            import hashlib
            h = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
            s = f"{(s or 'Part')}_{h}"
        return s

    def _close_same_docs(self, app, safe: str, log) -> None:
        """CATIA 同一会话不允许两个同名文档，SaveAs 会报
        「已存在于会话中。请尝试另一名称。」开新零件前先关掉残留的同名文档。"""
        try:
            n = int(app.Documents.Count)
        except Exception:
            return
        for i in range(n, 0, -1):
            try:
                d = app.Documents.Item(i)
                nm = str(d.Name)
            except Exception:
                continue
            base = nm.rsplit(".", 1)[0]
            if base != safe:
                continue
            try:
                d.Close()
                log(f"关闭会话中残留的同名文档：{nm}")
            except Exception as e:
                log(f"关闭同名文档失败（{nm}）：{e}")

    def _resolve(self):
        if self.exe and os.path.exists(self.exe):
            return
        try:
            from ..compat import detect_catia
            info = detect_catia()
            if info.found:
                self.exe = info.extra.get("catstart", self.exe)
                self.env_name = self.env_name or info.extra.get("env_name", "")
                self.env_dir = self.env_dir or info.extra.get("env_dir", "")
        except Exception:
            pass

    def available(self) -> bool:
        self._resolve()
        return bool(self.exe) and os.path.exists(self.exe)

    def _ensure_app(self, log):
        import win32com.client as win32
        try:
            return win32.GetActiveObject("CATIA.Application")
        except Exception:
            pass
        log("启动 CATIA（首次启动约 40 秒）…")
        args = [self.exe, "-run", "CNEXT.exe"]
        if self.env_name:
            args += ["-env", self.env_name]
        if self.env_dir:
            args += ["-direnv", self.env_dir]
        # CATIA must NOT inherit our stdout/stderr pipes:
        # otherwise the pipe never reaches EOF and callers block until CATIA exits.
        subprocess.Popen(args, cwd=os.path.dirname(self.exe) or None,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(4)
            try:
                app = win32.GetActiveObject("CATIA.Application")
                try:
                    app.Visible = True
                except Exception:
                    pass
                return app
            except Exception:
                continue
        raise RuntimeError("CATIA 启动超时")

    @staticmethod
    def _draw(f2d, segs):
        """按 seg 描述画 CATIA 原生圆弧 / 圆 + 直线，不做点列采样。

        旧实现用 flatten(arc_step_deg=1.5) 把圆弧拆成 240 段直线：
          - 弦高误差让体积长期差 0.1%~0.2%；
          - bbox 会在圆弧顶点落在采样点之间时缩水。
        CATIA 的 CreateCircle(x, y, r, a0, a1) 本身就支持扇形弧，参数是弧度、
        逆时针；geom 的 arc 约定也是逆时针 a0->a1（度），直接映射即可。
        """
        import math as _m
        # 先画圆弧/圆，再画直线：草图内核在直线先落位时容易把弧端点
        # 吸附到直线上，导致轮廓开口（SW 侧实测过同类问题）。
        for s in segs:
            if not s:
                continue
            if s[0] == "circle":
                _, cx, cy, r = s[:4]
                if r > 1e-12:
                    f2d.CreateCircle(cx, cy, r, 0.0, 2.0 * _m.pi)
            elif s[0] == "arc":
                _, cx, cy, r, a0, a1 = s[:6]
                if r <= 1e-12:
                    continue
                t0 = _m.radians(a0)
                t1 = _m.radians(a1)
                while t1 <= t0:
                    t1 += 2.0 * _m.pi
                f2d.CreateCircle(cx, cy, r, t0, t1)
        for s in segs:
            if not s or s[0] != "line":
                continue
            _, x1, y1, x2, y2 = s[:5]
            if abs(x1 - x2) < 1e-12 and abs(y1 - y2) < 1e-12:
                continue
            f2d.CreateLine(x1, y1, x2, y2)

    @staticmethod
    def _warmup(app, log, tries: int = 6):
        """CATIA 首次调用偶发 'Update 失败'，用一小段草图做预热。"""
        for i in range(tries):
            d = None
            try:
                d = app.Documents.Add("Part")
                try:
                    d.Activate()
                except Exception:
                    pass
                p = d.Part
                sk = p.MainBody.Sketches.Add(p.OriginElements.PlaneXY)
                f2 = sk.OpenEdition()
                f2.CreateCircle(0.0, 0.0, 5.0, 0.0, 6.283185)
                sk.CloseEdition()
                p.Update()
                p.ShapeFactory.AddNewPad(sk, 1.0)
                p.Update()
                try:
                    d.Close()
                except Exception:
                    pass
                if i:
                    log(f"CATIA 预热 {i + 1} 次后可用")
                return True
            except Exception:
                try:
                    d.Close()
                except Exception:
                    pass
                time.sleep(3)
        return False

    def _edge_cut(self, part, body, plane_el, offset, tris, depth, hsf):
        """在指定平面的偏置面上画截面并贯通切除（倒角用）。"""
        ref = part.CreateReferenceFromObject(plane_el)
        pl = hsf.AddNewPlaneOffset(ref, offset, False)
        try:
            body.InsertHybridShape(pl)
        except Exception:
            pass
        part.Update()
        s = body.Sketches.Add(part.CreateReferenceFromObject(pl))
        f = s.OpenEdition()
        for (a, b, c) in tris:
            f.CreateLine(a[0], a[1], b[0], b[1])
            f.CreateLine(b[0], b[1], c[0], c[1])
            f.CreateLine(c[0], c[1], a[0], a[1])
        s.CloseEdition()
        part.Update()
        part.ShapeFactory.AddNewPocket(s, depth)
        part.Update()

    @staticmethod
    def _inner_outline(spec, t):
        """抽壳内腔轮廓（顶壁要切掉的那块）：外轮廓向内偏 t。"""
        if spec.base_kind == "round":
            return [("circle", 0.0, 0.0, max(spec.diameter / 2.0 - t, 0.2))]
        L = max(spec.length - 2.0 * t, 0.2)
        W = max(spec.width - 2.0 * t, 0.2)
        left = str(getattr(spec, "end_left", "square") or "square").lower()
        right = str(getattr(spec, "end_right", "square") or "square").lower()
        if left != "arc" and right != "arc":
            return rect_outline(L, W, max(spec.corner_radius - t, 0.0))
        r = W / 2.0
        xi = max(0.0, L / 2.0 - r)
        xl = -xi if left == "arc" else -L / 2.0
        xr = xi if right == "arc" else L / 2.0
        segs = [("line", xl, -r, xr, -r)]
        segs.append(("arc", xi, 0.0, r, -90.0, 90.0) if right == "arc"
                    else ("line", xr, -r, xr, r))
        segs.append(("line", xr, r, xl, r))
        segs.append(("arc", -xi, 0.0, r, 90.0, 270.0) if left == "arc"
                    else ("line", xl, r, xl, -r))
        return segs

    def _shell_body(self, doc, part, body, spec, log):
        """抽壳：AddNewShell 需要"要移除的面"；CATIA 里平面引用不行。

        等价做法：先全封闭抽壳（iFaceToRemove = Nothing 可用），
        开口朝上时再把顶壁按内腔轮廓铣掉（深 t）。实测与解析体积零差。
        """
        t = float(getattr(spec, "shell_t", 0.0) or 0.0)
        if t <= 0:
            return True
        T = float(spec.thickness)
        if t >= min(T, spec.length or T, spec.width or T) / 2.0:
            log(f"⚠ 抽壳 t{t:g} 超过壁厚一半，跳过")
            return False
        sf = part.ShapeFactory
        try:
            sf.AddNewShell(None, t, 0.0)
            part.Update()
        except Exception as e0:
            log(f"⚠ 抽壳失败：{e0}")
            return False
        open_top = str(getattr(spec, "shell_open", "top") or "top").lower() != "none"
        if open_top:
            try:
                hsf = part.HybridShapeFactory
                ref0 = part.CreateReferenceFromObject(part.OriginElements.PlaneXY)
                pl = hsf.AddNewPlaneOffset(ref0, T, False)
                try:
                    body.InsertHybridShape(pl)
                except Exception:
                    pass
                part.Update()
                segs = self._inner_outline(spec, t)
                sk = body.Sketches.Add(part.CreateReferenceFromObject(pl))
                f2 = sk.OpenEdition()
                if segs[0][0] == "circle":
                    _, cx, cy, r = segs[0]
                    f2.CreateCircle(cx, cy, r, 0.0, 2 * math.pi)
                else:
                    self._draw(f2, segs)
                sk.CloseEdition()
                part.Update()
                sf.AddNewPocket(sk, t + 0.01)
                part.Update()
            except Exception as e0:
                log(f"⚠ 抽壳开口失败：{e0}")
                return False
        log(f"抽壳 t{t:g} 完成（{'开口朝上' if open_top else '全封闭'}）")
        return True

    def _fillet_rect(self, part, body, spec, log):
        """矩形板顶面外轮廓圆角 R：用"角部月牙截面 + 法向凹槽"精确切除。

        CATIA V5R21 的 COM 拿不到稳定可用的边引用（Selection.Search 直接 E_FAIL，
        Body 也没有 Edges 集合），所以不用 EdgeFillet，改为构造与圆角等效的
        截面切除：每个角一块"方形角 − 四分之一圆"的月牙，沿棱的法向贯通切掉。
        实测 100x60x12 / R3：体积 71391 vs 理论 71382（0.013%），长宽高 100/60/12 精确。
        """
        import math as _m
        r = spec.fillet
        L, W, T = spec.length, spec.width, spec.thickness
        hsf = part.HybridShapeFactory

        # 角部月牙 = 直角角块 - 四分之一圆（截面）。四分之一圆用 CATIA
        # 原生弧 CreateCircle(cx, cy, r, a0, a1)，不再用 32 段折线采样：
        # 折线会把 R 角截面做小（弦高误差），体积长期差 0.1%~0.2%。

        def chip_y(y0, inward, cap):
            """YZ 截面（u=y, v=z）的角部月牙，返回 seg 列表。

            C=(y0,T) 角点、B=(y0,T-cap)、A=(c0,T)，c0 = y0 + inward*cap。
            圆心 (c0, T-cap)：A 在 90 度，B 在 180 度（inward>0）或 0 度。
            """
            c0 = y0 + inward * cap
            if inward > 0:
                arc = ("arc", c0, T - cap, cap, 90.0, 180.0)
            else:
                arc = ("arc", c0, T - cap, cap, 0.0, 90.0)
            return [arc,
                    ("line", y0, T - cap, y0, T),
                    ("line", y0, T, c0, T)]

        def chip_x(x0, inward, cap):
            """ZX 截面（u=z, v=x）的角部月牙（坐标系 (Z, X)，符号与 YZ 相反）。"""
            c0 = x0 + inward * cap
            if inward > 0:
                arc = ("arc", T - cap, c0, cap, -90.0, 0.0)
            else:
                arc = ("arc", T - cap, c0, cap, 0.0, 90.0)
            return [arc,
                    ("line", T - cap, x0, T, x0),
                    ("line", T, x0, T, c0)]

        def cut_one(plane_el, offset, segs, depth):
            ref = part.CreateReferenceFromObject(plane_el)
            pl = hsf.AddNewPlaneOffset(ref, offset, False)
            try:
                body.InsertHybridShape(pl)
            except Exception:
                pass
            part.Update()
            s = body.Sketches.Add(part.CreateReferenceFromObject(pl))
            f = s.OpenEdition()
            self._draw(f, segs)
            s.CloseEdition()
            part.Update()
            part.ShapeFactory.AddNewPocket(s, depth)
            part.Update()

        try:
            # 每个角单独一张草图 + 一个凹槽：V5R21 把多条轮廓并到同一张草图里会算错区域
            cut_one(part.OriginElements.PlaneYZ, L / 2.0, chip_y(-W / 2.0, 1.0, r), L + 2)
            cut_one(part.OriginElements.PlaneYZ, L / 2.0, chip_y(W / 2.0, -1.0, r), L + 2)
            cut_one(part.OriginElements.PlaneZX, W / 2.0, chip_x(-L / 2.0, 1.0, r), W + 2)
            cut_one(part.OriginElements.PlaneZX, W / 2.0, chip_x(L / 2.0, -1.0, r), W + 2)
            log(f"圆角 R{r:g} 完成（顶面外轮廓四边，截面法）")
        except Exception as e:
            log(f"圆角失败：{e}")

    def _chamfer_rect(self, part, body, spec, log):
        """矩形板四边倒角：沿两条轴的截面三角 + 双向贯通凹槽。"""
        c = spec.chamfer
        L, W, T = spec.length, spec.width, spec.thickness
        hsf = part.HybridShapeFactory
        try:
            self._edge_cut(part, body, part.OriginElements.PlaneYZ, L / 2.0,
                           [((-W / 2, T - c), (-W / 2 + c, T), (-W / 2, T)),
                            ((W / 2, T - c), (W / 2 - c, T), (W / 2, T))], L + 2, hsf)
            self._edge_cut(part, body, part.OriginElements.PlaneZX, W / 2.0,
                           [((T - c, -L / 2), (T, -L / 2 + c), (T, -L / 2)),
                            ((T - c, L / 2), (T, L / 2 - c), (T, L / 2))], W + 2, hsf)
            log(f"倒角 C{c:g} 完成（四边）")
        except Exception as e:
            log(f"倒角失败：{e}")

    def _chamfer_round(self, part, body, spec, log, res):
        """圆盘外圆顶棱倒角：绕竖轴回转三角环切除，不依赖边引用。

        截面三角形（草图 u=半径，v=高度）：
            A = (R-c-1, T+1)   左上，顶面之上的空处
            B = (R+1,   T+1)   右上角，实体之外
            C = (R+1,   T-c-1) 右下，外圆之外的空处
        斜边 A→C 斜率 -1，经过 (R-c, T) 与 (R, T-c)，即标准 45° 倒角面。
        另两条边完全在实体外，回转切除不会多切。

        去料解析值：V = π c² (R - c/3)
        （与 SW 实测对照：R=60,c=1 → 187.44 对 187.448；R=52,c=2 → 645.06 对 645.074）
        """
        c = float(spec.chamfer)
        T = float(spec.thickness)
        R = float(spec.diameter) / 2.0
        if c <= 0 or R <= 0:
            return

        v0 = self._measure_volume(part.Parent, part, body)
        if v0 <= 0:
            log("⚠ 圆盘倒角：切除前体积测不到，跳过")
            res.details["chamfer_skipped"] = "圆盘倒角跳过：切除前体积测量失败"
            return

        # 倒角不能吃掉整个外圆：c 必须小于半径且小于厚度
        c_eff = min(c, R * 0.9, T * 0.9)
        if c_eff < c - 1e-9:
            log(f"⚠ 倒角 C{c:g} 超出几何允许范围，按 C{c_eff:g} 执行")

        tri = [(R - c_eff - 1.0, T + 1.0),
               (R + 1.0, T + 1.0),
               (R + 1.0, T - c_eff - 1.0)]
        sk = None
        try:
            sk = body.Sketches.Add(part.OriginElements.PlaneYZ)
            f = sk.OpenEdition()
            for i in range(3):
                a = tri[i]
                b = tri[(i + 1) % 3]
                f.CreateLine(a[0], a[1], b[0], b[1])
            axis = f.CreateLine(0.0, T + 2.0, 0.0, -2.0)
            try:
                sk.CenterLine = axis
            except Exception as e_ax:
                log(f"⚠ 圆盘倒角草图轴线设置失败：{e_ax}")
            sk.CloseEdition()
            part.Update()
            part.ShapeFactory.AddNewGroove(sk)
            part.Update()
        except Exception as e:
            try:
                if sk is not None:
                    sk.CloseEdition()
            except Exception:
                pass
            log(f"⚠ 圆盘倒角失败：{str(e)[:140]}")
            res.details["chamfer_skipped"] = f"CATIA 圆盘倒角失败：{str(e)[:100]}"
            return

        v1 = self._measure_volume(part.Parent, part, body)
        removed = v0 - v1
        v_exp = math.pi * c_eff * c_eff * (R - c_eff / 3.0)
        if v1 <= 0:
            log("⚠ 圆盘倒角：切除后体积测不到，无法确认去料")
            res.details["chamfer_skipped"] = "圆盘倒角已建但体积无法复测"
            return
        if removed <= 0:
            # 建成了但一点料没去 —— 这正是 NX 那次踩过的坑，必须喊出来
            log(f"⚠ 圆盘倒角 C{c_eff:g}：特征已建但去料 {removed:,.1f} mm³，疑似未生效")
            res.details["chamfer_failed"] = (
                f"CATIA 圆盘倒角 C{c_eff:g} 建成但未去料（{removed:,.1f} mm³）")
            res.ok = False
            return
        dev = (removed - v_exp) / v_exp * 100.0 if v_exp > 0 else 0.0
        log(f"倒角 C{c_eff:g} 完成（外圆顶棱，回转切除），"
            f"去料 {removed:,.1f} mm³（解析 {v_exp:,.1f}，差 {dev:+.2f}%）")
        if abs(dev) > 20.0:
            log(f"⚠ 圆盘倒角去料与解析差 {dev:+.1f}%，请核对倒角所在圆周半径"
                f"（按 R={R:g} 计算；若倒角其实在凸台外圆上，会偏小）")
            res.details["chamfer_round_deviation"] = round(dev, 2)
        res.details["chamfer_removed"] = round(removed, 3)

    def _measure_bbox(self, doc, part, body):
        """实体包围盒（mm），返回 (xmin,ymin,zmin,xmax,ymax,zmax) 或 None。

        STEP 里 CATIA 把回转面导成 B 样条，经向圆弧的极值取不到，
        所以回转型零件用 CAD 自己的包围盒核对外形。
        """
        try:
            import pythoncom
            from win32com.client import VARIANT
        except Exception:
            VARIANT = None
        try:
            wb = doc.GetWorkbench("SPAWorkbench")
            m = wb.GetMeasurable(part.CreateReferenceFromObject(body))
        except Exception:
            return None
        if VARIANT is not None:
            try:
                lo = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_ARRAY | pythoncom.VT_R8,
                             [0.0, 0.0, 0.0])
                hi = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_ARRAY | pythoncom.VT_R8,
                             [0.0, 0.0, 0.0])
                m.GetBoundingBox(lo, hi)
                vals = list(lo.value) + list(hi.value)
                if len(vals) == 6 and abs(vals[3] - vals[0]) > 0:
                    return tuple(float(v) * 1000.0 for v in vals)
            except Exception:
                pass
        try:
            lo, hi = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
            m.GetBoundingBox(lo, hi)
            vals = list(lo) + list(hi)
            if len(vals) == 6 and abs(vals[3] - vals[0]) > 0:
                return tuple(float(v) * 1000.0 for v in vals)
        except Exception:
            pass
        return None

    def _measure_volume(self, doc, part, body):
        """实体体积（mm³），SPAWorkbench 实测。"""
        try:
            wb = doc.GetWorkbench("SPAWorkbench")
            m = wb.GetMeasurable(part.CreateReferenceFromObject(body))
            return float(m.Volume) * 1e9
        except Exception:
            return 0.0

    def _revolve(self, part, body, spec, log):
        """通用回转基体：原生直线/真圆弧母线绕竖轴 360°（不做点列拟合）。

        草图放在 PlaneYZ：u = 半径 r，v = 高度 z，与 _dome 同一套坐标。
        返回 (ok, err)；建完用 SPAWorkbench 实测体积，与母线解析体积对比。
        """
        prof = [q for q in (getattr(spec, "revolve_profile", None) or []) if isinstance(q, dict)]
        if len(prof) < 3:
            return False, "回转母线少于 3 段"
        from ..calc import revolve_volume
        zs = [float(q["p1"][1]) for q in prof] + [float(q["p2"][1]) for q in prof]
        zmin, zmax = min(zs), max(zs)
        sk = body.Sketches.Add(part.OriginElements.PlaneYZ)
        f2 = sk.OpenEdition()
        n_line = n_arc = 0
        try:
            for seg in prof:
                a = seg.get("p1") or (0.0, 0.0)
                b = seg.get("p2") or (0.0, 0.0)
                if str(seg.get("type")) == "arc":
                    c = seg.get("center") or (0.0, 0.0)
                    r = math.hypot(float(a[0]) - float(c[0]), float(a[1]) - float(c[1]))
                    t1 = math.atan2(float(a[1]) - float(c[1]), float(a[0]) - float(c[0]))
                    t2 = math.atan2(float(b[1]) - float(c[1]), float(b[0]) - float(c[0]))
                    d = (t2 - t1) % (2.0 * math.pi)
                    if int(seg.get("direction") or 1) >= 0:
                        s0, e0 = t1, t1 + d
                    else:
                        s0, e0 = t1, t1 - (2.0 * math.pi - d)
                    # 圆 = 等半径椭圆：本机 CreateCircle 带非零起始角会生成无效草图
                    f2.CreateEllipse(float(c[0]), float(c[1]), 1.0, 0.0, r, r, s0, e0)
                    n_arc += 1
                else:
                    f2.CreateLine(float(a[0]), float(a[1]), float(b[0]), float(b[1]))
                    n_line += 1
            axis = f2.CreateLine(0.0, zmax + 1.0, 0.0, zmin - 1.0)
            try:
                sk.CenterLine = axis
            except Exception as e_ax:
                log(f"⚠ 回转草图轴线设置失败：{e_ax}")
        except Exception as e:
            try:
                sk.CloseEdition()
            except Exception:
                pass
            return False, f"回转母线绘制失败：{str(e)[:120]}"
        sk.CloseEdition()
        part.Update()
        try:
            part.ShapeFactory.AddNewShaft(sk)
            part.Update()
        except Exception as e:
            return False, f"回转特征失败：{str(e)[:120]}"
        v_cad = self._measure_volume(part.Parent, part, body)
        v_exp = float(revolve_volume(spec) or 0.0)
        if v_cad <= 0:
            return False, "回转基体建成但体积测量为 0"
        if v_exp > 0 and abs(v_cad - v_exp) / v_exp > 0.01:
            return False, (f"回转实测 {v_cad:,.1f} 与母线解析 {v_exp:,.1f} 差 "
                           f"{(v_cad - v_exp) / v_exp * 100.0:.2f}%")
        log(f"回转基体完成：母线 {len(prof)} 段（直线 {n_line} + 真圆弧 {n_arc}）"
            f"绕竖轴 360°，实测 {v_cad:,.1f} mm³（母线解析 {v_exp:,.1f}）")
        return True, ""

    def _dome(self, part, body, spec, log):
        """圆顶面：CATIA 原生椭圆/圆弧 + Shaft 回转加料，不做点列拟合。

        椭圆顶 = Factory2D.CreateEllipse 四分之一椭圆弧（半轴 a×h）绕竖轴回转；
        球冠   = 原生圆弧（R=(a²+h²)/2h）绕竖轴回转。
        返回 (新增料 mm³, 错误信息)；<=1 表示没做成。
        """
        h = float(getattr(spec, "dome_h", 0.0) or 0.0)
        if h <= 0:
            return 0.0, ""
        T = float(spec.thickness)
        ell = bool(getattr(spec, "dome_elliptic", True))
        if spec.base_kind == "round":
            a = float(spec.diameter) / 2.0
        else:
            a = min(float(spec.length or 0.0), float(spec.width or 0.0)) / 2.0
        if a <= 0:
            return 0.0, "基体半径无效"

        part.Update()
        try:
            wb = part.Parent.GetWorkbench("SPAWorkbench")
            m0 = wb.GetMeasurable(part.CreateReferenceFromObject(body))
            v0 = float(m0.Volume) * 1e9
        except Exception:
            v0 = 0.0

        zb = T
        sk = body.Sketches.Add(part.OriginElements.PlaneYZ)
        f2 = sk.OpenEdition()
        try:
            if ell:
                # 四分之一椭圆：中心(0,T)，长半轴 a 沿 u，短半轴 h 沿 v(=Z)
                # CATIA: CreateEllipse(cx, cy, majorX, majorY, majorR, minorR, start, end)
                f2.CreateEllipse(0.0, zb, 1.0, 0.0, a, h, 0.0, math.pi / 2.0)
            else:
                R = (a * a + h * h) / (2.0 * h)
                vc = zb - (R - h)              # 圆心在轮缘一侧
                ang0 = math.atan2(R - h, a)    # 轮缘点 (a,zb)
                # 圆也是椭圆（长短半轴相等）；CATIA 的 CreateCircle 参数化在
                # 本机对非 0 起始角会生成无效草图，改用同一套 CreateEllipse。
                f2.CreateEllipse(0.0, vc, 1.0, 0.0, R, R, ang0, math.pi / 2.0)
            # 底边必须显式闭合：CATIA 的 Shaft 对开放母线会报 Update 失败
            # （实测：同一圆弧，缺底边必失败，补上底边即成功）。
            f2.CreateLine(0.0, zb, a, zb)
            axis = f2.CreateLine(0.0, zb + h, 0.0, zb)
            try:
                sk.CenterLine = axis
            except Exception as e_ax:
                log(f"⚠ 圆顶草图轴线设置失败：{e_ax}")
        except Exception as e:
            try:
                sk.CloseEdition()
            except Exception:
                pass
            return 0.0, str(e)[:120]
        sk.CloseEdition()
        part.Update()
        try:
            sh = part.ShapeFactory.AddNewShaft(sk)
            part.Update()
        except Exception as e:
            return 0.0, str(e)[:120]

        v1 = self._measure_volume(part.Parent, part, body)
        dv = v1 - v0
        v_theory = ((2.0 / 3.0) * math.pi * a * a * h if ell
                    else math.pi * h * (3.0 * a * a + h * h) / 6.0)
        if v0 > 0 and v1 > 0 and v_theory > 0:
            if abs(dv - v_theory) / v_theory > 0.01:
                return 0.0, (f"实测料 {dv:,.1f} 与理论 {v_theory:,.1f} 差 "
                             f"{(dv - v_theory) / v_theory * 100.0:.2f}%")
        log(f"圆顶面完成：h={h:g}（{'elliptic' if ell else 'spherical'}），"
            f"实测料 +{dv:,.1f} mm³（理论 {v_theory:,.1f}）")
        return dv, ""

    def _sweep(self, part, body, spec, log) -> int:
        """原生扫掠（CATIA Rib）：截面草图=矩形，中心线=真直线+真圆弧。

        只支持路径整体落在 XZ 平面（所有点同一 Y）、首段切向 ±X 的情形。
        CATIA 的 PlaneZX 局部坐标 u=Z、v=X（是镜像），世界 XZ 里的逆时针
        圆弧到草图里变成顺时针；实测 Rib 只接受 CreateCircle(a0<a1) 的
        逆时针弧，所以这里按局部逆时针画（必要时从终点画到起点），草图
        内核会把线段与圆弧自动接成中心线。全程不用点列拟合。
        """
        sws = list(getattr(spec, "sweeps", None) or [])
        if not sws:
            return 0
        made = 0
        for idx, item in enumerate(sws, 1):
            w = float(item.get("profile_w") or 0.0)
            h = float(item.get("profile_h") or 0.0)
            path = [dict(x) for x in (item.get("path") or [])]
            if w <= 0 or h <= 0 or not path:
                log(f"⚠ 扫掠 {idx}：截面或路径为空，跳过")
                continue
            pts = []
            for seg in path:
                for k in ("p1", "p2", "center"):
                    q = seg.get(k)
                    if q:
                        pts.append([float(v) for v in q[:3]])
            if not pts:
                continue
            ys = [q[1] for q in pts]
            if max(ys) - min(ys) > 1e-6:
                log(f"⚠ 扫掠 {idx}：路径不在 XZ 平面，暂不支持")
                continue
            y0 = ys[0]

            def _ends(seg):
                a = [float(v) for v in (seg.get("p1") or [0, 0, 0])[:3]]
                b = [float(v) for v in (seg.get("p2") or [0, 0, 0])[:3]]
                return a, b

            a0, b0 = _ends(path[0])
            dx, dz = b0[0] - a0[0], b0[2] - a0[2]
            if abs(dx) < 1e-9 or abs(dx) < abs(dz):
                log(f"⚠ 扫掠 {idx}：起始切向不是 ±X，暂不支持")
                continue
            x_start, z_start = a0[0], a0[2]

            hsf = part.HybridShapeFactory
            # ---- 路径草图：PlaneZX 偏移到 y0（局部 u=Z、v=X）
            ref_zx = part.CreateReferenceFromObject(part.OriginElements.PlaneZX)
            if abs(y0) > 1e-9:
                pp = hsf.AddNewPlaneOffset(ref_zx, y0, False)
                try:
                    body.InsertHybridShape(pp)
                except Exception:
                    pass
                part.Update()
                pref = part.CreateReferenceFromObject(pp)
            else:
                pref = part.OriginElements.PlaneZX
            try:
                sp = body.Sketches.Add(pref)
                fp = sp.OpenEdition()
                nseg = 0
                for seg in path:
                    typ = str(seg.get("type") or "line").lower()
                    a, b = _ends(seg)
                    ua, va = a[2], a[0]
                    ub, vb = b[2], b[0]
                    if typ == "arc":
                        c = [float(v) for v in (seg.get("center") or [0, 0, 0])[:3]]
                        uc, vc = c[2], c[0]
                        r = math.hypot(ua - uc, va - vc)
                        if r <= 1e-9:
                            continue
                        a_s = math.atan2(a[2] - c[2], a[0] - c[0])
                        a_e = math.atan2(b[2] - c[2], b[0] - c[0])
                        dirw = int(seg.get("direction") or 1)
                        if dirw >= 0:
                            span_w = (a_e - a_s) % (2.0 * math.pi)
                        else:
                            span_w = -((a_s - a_e) % (2.0 * math.pi))
                        span_l = -span_w
                        t_a = math.atan2(va - vc, ua - uc)
                        t_b = math.atan2(vb - vc, ub - uc)
                        if span_l >= 0:
                            fp.CreateCircle(uc, vc, r, t_a, t_a + span_l)
                        else:
                            fp.CreateCircle(uc, vc, r, t_b, t_b - span_l)
                    else:
                        if abs(ua - ub) < 1e-12 and abs(va - vb) < 1e-12:
                            continue
                        fp.CreateLine(ua, va, ub, vb)
                    nseg += 1
                sp.CloseEdition()
                part.Update()
            except Exception as e:
                log(f"⚠ 扫掠 {idx}：路径草图失败：{str(e)[:120]}")
                continue

            # ---- 截面草图：PlaneYZ 偏移到 x_start（局部 u=Y、v=Z）
            try:
                ref_yz = part.CreateReferenceFromObject(part.OriginElements.PlaneYZ)
                if abs(x_start) > 1e-9:
                    qp = hsf.AddNewPlaneOffset(ref_yz, x_start, False)
                    try:
                        body.InsertHybridShape(qp)
                    except Exception:
                        pass
                    part.Update()
                    qref = part.CreateReferenceFromObject(qp)
                else:
                    qref = part.OriginElements.PlaneYZ
                s2 = body.Sketches.Add(qref)
                f2 = s2.OpenEdition()
                hu, hv = w / 2.0, h / 2.0
                f2.CreateLine(y0 - hu, z_start - hv, y0 + hu, z_start - hv)
                f2.CreateLine(y0 + hu, z_start - hv, y0 + hu, z_start + hv)
                f2.CreateLine(y0 + hu, z_start + hv, y0 - hu, z_start + hv)
                f2.CreateLine(y0 - hu, z_start + hv, y0 - hu, z_start - hv)
                s2.CloseEdition()
                part.Update()
            except Exception as e:
                log(f"⚠ 扫掠 {idx}：截面草图失败：{str(e)[:120]}")
                continue

            try:
                rib = part.ShapeFactory.AddNewRib(s2, sp)
                part.Update()
                made += 1
                nm = getattr(rib, "Name", "") or ""
                log(f"扫掠 {idx} 完成：截面 {w:g}×{h:g}，路径 {nseg} 段"
                    f"（真直线+真圆弧）" + (f"：{nm}" if nm else ""))
            except Exception as e:
                log(f"⚠ 扫掠 {idx}：原生 Rib 失败：{str(e)[:160]}")
        return made

    def build(self, spec: PartSpec, out_dir: str,
              log: Callable[[str], None] = lambda m: None) -> BuildResult:
        t0 = time.time()
        res = BuildResult(cad="CATIA", expected=0.0)
        _ok, _msg = _mem_check(1200.0, 92.0, "CATIA")
        if not _ok:
            # 内存不够时，先把**其它** CAD 关掉（用户要求同时只开一个），
            # 再重判一次；否则 SW 占着的 500+ MB 会让 CATIA/NX 永远起不来。
            try:
                from .guard import free_others as _free_others
                _free_others("CATIA", log, fast=True)
            except Exception:
                pass
            _ok, _msg = _mem_check(1200.0, 92.0, "CATIA")
        if not _ok:
            res.message = _msg
            log("\u26a0 " + _msg)
            res.elapsed = time.time() - t0
            return res
        if _msg:
            log(_msg)
        # COM 库只在内存闸门放行后再导入：低内存时连 pythoncom 都不碰。
        import pythoncom
        import win32com.client as win32

        res.expected = part_volume(spec)
        out = Path(out_dir).resolve()
        out.mkdir(parents=True, exist_ok=True)
        pythoncom.CoInitialize()
        doc = None
        try:
            _cad_acquire("CATIA", log)
            log("连接 CATIA…")
            self._resolve()
            app = self._ensure_app(log)
            try:
                app.Visible = True
            except Exception:
                pass

            self._warmup(app, log)
            safe = self._safe_name(spec.part_name)
            self._close_same_docs(app, safe, log)
            doc = app.Documents.Add("Part")
            try:
                doc.Activate()
            except Exception:
                pass
            part = doc.Part
            body = part.MainBody
            T = spec.thickness

            # ---------------- sheet metal
            if spec.sheet_t > 0 and spec.sheet_L > 0:
                L = spec.sheet_L; W = spec.sheet_W; th = spec.sheet_t; h = spec.flange_h
                def rect_pad(cx, cy, l, w, depth):
                    s_ = body.Sketches.Add(part.OriginElements.PlaneXY)
                    f_ = s_.OpenEdition()
                    from ..geom import rect_outline as _ro
                    self._draw(f_, _ro(l, w, 0.0, cx, cy))
                    s_.CloseEdition()
                    part.Update()
                    part.ShapeFactory.AddNewPad(s_, depth)
                    part.Update()
                rect_pad(0, 0, L, W, th)
                rect_pad(0, W / 2.0 - th / 2.0, L, th, h)
                rect_pad(0, -(W / 2.0 - th / 2.0), L, th, h)
                log(f"sheet: base {L:g}x{W:g}x{th:g}, flange h={h:g}")

            # ---------------- gear
            if spec.teeth > 0 and spec.module > 0:
                from ..gear import gear_profile
                pts_g = gear_profile(spec.module, spec.teeth)
                sk_g = body.Sketches.Add(part.OriginElements.PlaneXY)
                f2g = sk_g.OpenEdition()
                for i in range(len(pts_g)):
                    a = pts_g[i]; b = pts_g[(i + 1) % len(pts_g)]
                    f2g.CreateLine(a[0], a[1], b[0], b[1])
                sk_g.CloseEdition()
                part.Update()
                part.ShapeFactory.AddNewPad(sk_g, spec.face_width)
                part.Update()
                log(f"齿轮齿廓 {len(pts_g)} 点，齿宽 {spec.face_width:g}")
                hsf_g = part.HybridShapeFactory
                ref_g = part.CreateReferenceFromObject(part.OriginElements.PlaneXY)
                pl_g = hsf_g.AddNewPlaneOffset(ref_g, spec.face_width, False)
                try:
                    body.InsertHybridShape(pl_g)
                except Exception:
                    pass
                part.Update()
                pl_g_ref = part.CreateReferenceFromObject(pl_g)
                if spec.bore > 0:
                    sk_h = body.Sketches.Add(pl_g_ref)
                    f2h = sk_h.OpenEdition()
                    f2h.CreateCircle(0.0, 0.0, spec.bore / 2.0, 0.0, 2 * math.pi)
                    sk_h.CloseEdition()
                    part.Update()
                    part.ShapeFactory.AddNewPocket(sk_h, spec.face_width + 1.0)
                    part.Update()
                    log(f"轴孔 D{spec.bore:g}")
                if spec.keyway_w > 0 and spec.keyway_d > 0:
                    w = spec.keyway_w / 2.0
                    y0 = spec.bore / 2.0 - spec.keyway_d
                    y1 = spec.bore / 2.0 + spec.keyway_d
                    sk_k = body.Sketches.Add(pl_g_ref)
                    f2k = sk_k.OpenEdition()
                    f2k.CreateLine(-w, y0, w, y0)
                    f2k.CreateLine(w, y0, w, y1)
                    f2k.CreateLine(w, y1, -w, y1)
                    f2k.CreateLine(-w, y1, -w, y0)
                    sk_k.CloseEdition()
                    part.Update()
                    part.ShapeFactory.AddNewPocket(sk_k, spec.face_width + 1.0)
                    part.Update()
                    log("keyway cut")

            # ---------------- shaft (回转体)
            if spec.shaft_segments:
                hsf0 = part.HybridShapeFactory
                ref0 = part.CreateReferenceFromObject(part.OriginElements.PlaneXY)
                z = 0.0
                for i, seg in enumerate(spec.shaft_segments):
                    d = float(seg.get("dia") or 10.0)
                    L = float(seg.get("length") or 10.0)
                    if i == 0:
                        sk_s = body.Sketches.Add(part.OriginElements.PlaneXY)
                    else:
                        pl = hsf0.AddNewPlaneOffset(ref0, z, False)
                        try:
                            body.InsertHybridShape(pl)
                        except Exception:
                            pass
                        part.Update()
                        sk_s = body.Sketches.Add(part.CreateReferenceFromObject(pl))
                    f2s = sk_s.OpenEdition()
                    f2s.CreateCircle(0.0, 0.0, d / 2.0, 0.0, 2 * math.pi)
                    sk_s.CloseEdition()
                    part.Update()
                    part.ShapeFactory.AddNewPad(sk_s, L)
                    part.Update()
                    z += L
                log(f"回转体完成：{len(spec.shaft_segments)} 段，总长 {z:g}")

            # ---------------- base
            revolve_like = bool(getattr(spec, "revolve_profile", None))
            shaft_like = bool(spec.shaft_segments) or (spec.teeth > 0 and spec.module > 0) \
                or (spec.sheet_t > 0 and spec.sheet_L > 0) or revolve_like
            sk = body.Sketches.Add(part.OriginElements.PlaneXY) if not shaft_like else None
            if shaft_like:
                f2d = None
            else:
              f2d = sk.OpenEdition()
              if spec.base_kind == "round":
                f2d.CreateCircle(0.0, 0.0, spec.diameter / 2.0, 0.0, 2 * math.pi)
                log(f"绘制圆盘 Ø{spec.diameter:g}")
              else:
                from ..calc import base_outline as _bo
                self._draw(f2d, _bo(spec))
                _el = str(getattr(spec, "end_left", "square") or "square").lower()
                _er = str(getattr(spec, "end_right", "square") or "square").lower()
                _tail = ""
                if _el == "arc" or _er == "arc":
                    _tail = f"，{_el} 端圆弧头"
                log(f"绘制轮廓 {spec.length:g}×{spec.width:g}"
                    f"（圆角 R{spec.corner_radius:g}{_tail}）")
            if revolve_like:
                _okr, _errr = self._revolve(part, body, spec, log)
                if not _okr:
                    raise RuntimeError(_errr or "回转基体未生成")
            if not shaft_like:
                sk.CloseEdition()
                part.Update()
                pad = part.ShapeFactory.AddNewPad(sk, T)
                part.Update()
                log(f"凸台完成：{pad.Name}")
                if float(getattr(spec, "shell_t", 0.0) or 0.0) > 0:
                    self._shell_body(doc, part, body, spec, log)

            # ---------------- 圆顶面（在切除之前，顶面仍是完整平面）
            if float(getattr(spec, "dome_h", 0.0) or 0.0) > 0 and not shaft_like:
                _dv, _de = self._dome(part, body, spec, log)
                if _dv <= 1.0:
                    res.details["dome_error"] = _de or "圆顶面未生成"
                    log(f"⚠ 圆顶面失败：{res.details['dome_error']}")

            # ---------------- native sweeps (additive)
            if getattr(spec, "sweeps", None):
                _nsw = self._sweep(part, body, spec, log)
                res.details["sweep_built"] = f"{_nsw}/{len(spec.sweeps)}"
                if _nsw < len(spec.sweeps):
                    res.failures.append(f"扫掠 {_nsw}/{len(spec.sweeps)} 失败")

            # ---------------- top plane
            hsf = part.HybridShapeFactory
            ref0 = part.CreateReferenceFromObject(part.OriginElements.PlaneXY)
            top = hsf.AddNewPlaneOffset(ref0, T, False)
            try:
                body.InsertHybridShape(top)
            except Exception:
                pass
            part.Update()
            top_ref = part.CreateReferenceFromObject(top)

            def pocket(segs_or_circle, depth: float, name: str):
                s2 = body.Sketches.Add(top_ref)
                f2 = s2.OpenEdition()
                if segs_or_circle[0] == "circle":
                    _, cx, cy, r = segs_or_circle
                    f2.CreateCircle(cx, cy, r, 0.0, 2 * math.pi)
                else:
                    self._draw(f2, segs_or_circle)
                s2.CloseEdition()
                part.Update()
                pk = part.ShapeFactory.AddNewPocket(s2, depth)
                part.Update()
                log(f"凹槽完成：{name}（深 {depth:g}）")
                return pk

            # 先按大轮廓→小特征排序，并把薄壁冲突的孔外扩（见 app/conflict.py）
            for _w in spec.conflicts().get("warnings", []):
                log("⚠ " + _w)
            _feats = list(spec.build_features())
            _feats.sort(key=lambda ff: -max(ff.length or 0, ff.width or 0, ff.diameter or 0))
            for f in _feats:
                g = spec.grow_of(f)
                if f.type == "hole":
                    d = feature_depth(spec, f) if not f.through else T + 1.0
                    if f.through or f.depth <= 0:
                        d = T + 1.0
                    pocket(("circle", f.x, f.y, f.diameter / 2.0 + g), d, f"孔 Ø{f.diameter:g}")
                    if (f.counterbore_dia > f.diameter and f.counterbore_depth > 0
                            and not spec.counterbore_is_void(f)):
                        pocket(("circle", f.x, f.y, f.counterbore_dia / 2.0 + g),
                               f.counterbore_depth, f"沉孔 Ø{f.counterbore_dia:g}")
                elif f.type == "slot":
                    L, W = (f.length or f.width), f.width + 2 * g
                    seam = (("circle", f.x, f.y, W / 2.0) if L <= W + 1e-6
                            else slot_outline(L, W, f.x, f.y, f.angle))
                    pocket(seam, T + 1.0 if (f.through and f.depth <= 0) else feature_depth(spec, f),
                           f"腰形槽 {f.length:g}×{f.width:g}")
                elif f.type == "sphere":
                    # 球坑：原生回转切除（Groove）。草图放在 z=zc 平面，画半圆 +
                    # 直径线，直径线设为草图 CenterLine 作为回转轴，转 360° 得到
                    # 完整球体减去材料后留下的球冠/半球坑。不使用任何点列采样。
                    # 实测 R8 半球：切除量 1,072.3 = 2/3*pi*R^3，与解析值一致。
                    R = f.diameter / 2.0 + g
                    h = min(f.depth or (f.diameter / 2.0), f.diameter / 2.0)
                    zc = T - h + R
                    ref_xy = part.CreateReferenceFromObject(part.OriginElements.PlaneXY)
                    pl_s = hsf.AddNewPlaneOffset(ref_xy, zc, False)
                    try:
                        body.InsertHybridShape(pl_s)
                    except Exception:
                        pass
                    part.Update()
                    s_s = body.Sketches.Add(part.CreateReferenceFromObject(pl_s))
                    f_s = s_s.OpenEdition()
                    f_s.CreateCircle(f.x, f.y, R, 0.0, _math.pi)
                    axis_line = f_s.CreateLine(f.x - R, f.y, f.x + R, f.y)
                    try:
                        s_s.CenterLine = axis_line
                    except Exception as e_ax:
                        log(f"⚠ 球坑草图中线设置失败：{e_ax}")
                    s_s.CloseEdition()
                    part.Update()
                    gr = part.ShapeFactory.AddNewGroove(s_s)
                    part.Update()
                    log(f"球坑 SR{f.diameter / 2.0:g} 深 {h:g} 完成（原生回转切除）")
                elif f.type in ("pocket", "step", "boss"):
                    segs = rect_outline(f.length + 2 * g, f.width + 2 * g, f.corner_radius, f.x, f.y)
                    d = T + 1.0 if (f.through and f.depth <= 0) else feature_depth(spec, f)
                    pocket(segs, d, f"{f.type} {f.length:g}×{f.width:g}")

            # ---------------- chamfer (rect plates)
            if spec.chamfer > 0 and spec.fillet > 0:
                log(f"同一圈顶棱已有圆角 R{spec.fillet:g}，倒角 C{spec.chamfer:g} 被覆盖，跳过")
            if spec.chamfer > 0 and spec.fillet <= 0 and spec.base_kind == "rect" \
                    and not spec.shaft_segments and not spec.teeth and not (spec.sheet_t > 0):
                self._chamfer_rect(part, body, spec, log)
            elif spec.chamfer > 0 and spec.fillet <= 0 and spec.base_kind == "round" \
                    and not spec.shaft_segments and not spec.teeth and not (spec.sheet_t > 0):
                # 圆盘外圆倒角：绕竖轴回转三角环切除。
                # 旧版因"拿不到稳定边引用"直接跳过，导致体积比理论值偏高约 0.2%。
                # 现在把定位也几何化，不依赖边引用（详见 _chamfer_round）。
                if spec.diameter and spec.diameter > 0:
                    self._chamfer_round(part, body, spec, log, res)
                else:
                    res.details["chamfer_skipped"] = (
                        f"CATIA 未实现圆盘类倒角（C{spec.chamfer:g}）：该零件没给直径")
            if spec.fillet > 0 and spec.base_kind == "rect" and not spec.shaft_segments \
                    and not spec.teeth and not (spec.sheet_t > 0):
                self._fillet_rect(part, body, spec, log)

            # ---------------- measure + save
            def _vol(fresh=False):
                """实体体积（mm³），来自 SPAWorkbench 的实测，不是理论值。"""
                try:
                    if fresh:
                        part.Update()
                except Exception:
                    pass
                try:
                    wb2 = doc.GetWorkbench("SPAWorkbench")
                    m2 = wb2.GetMeasurable(part.CreateReferenceFromObject(body))
                    return float(m2.Volume) * 1e9
                except Exception:
                    return 0.0

            v_before_save = _vol()

            if safe != spec.part_name:
                log(f"CATIA 文件名不支持中文，改用：{safe}")
            part_path = out / f"{safe}.CATPart"
            step_path = out / f"{safe}.stp"
            # CATIA 遇到同名文件会弹「已经存在，要覆盖它吗？」模态框，
            # 阻塞 COM 调用并报「方法 SaveAs 失败」。先清掉旧文件，
            # 再保存；同时后台守护线程兜底点掉可能出现的覆盖框。
            for old in (part_path, step_path):
                try:
                    if old.exists():
                        old.chmod(0o666)
                        old.unlink()
                except Exception as e:
                    log(f"清理旧文件失败（{old.name}）：{e}")
            guard = _start_overwrite_guard(log)
            try:
                doc.SaveAs(str(part_path))
                res.part_path = str(part_path)
                log(f"已保存零件：{part_path.name}")
            except Exception as e:
                log(f"保存失败：{e}")
            finally:
                _stop_overwrite_guard(guard)

            # STEP 导出：覆盖框可能打断导出，重试一次
            for attempt in (1, 2):
                if step_path.exists():
                    try:
                        step_path.chmod(0o666)
                        step_path.unlink()
                    except Exception:
                        pass
                guard = _start_overwrite_guard(log)
                try:
                    doc.ExportData(str(step_path), "stp")
                    if step_path.exists() and step_path.stat().st_size > 0:
                        res.step_path = str(step_path)
                        log(f"已导出 STEP：{step_path.name}")
                    else:
                        log(f"STEP 导出后未生成文件（第 {attempt} 次）")
                except Exception as e:
                    log(f"STEP 导出失败（第 {attempt} 次）：{e}")
                finally:
                    _stop_overwrite_guard(guard)
                if res.step_path:
                    break
                time.sleep(1.0)

            try:
                viewer = app.ActiveWindow.ActiveViewer
                img = str(out / f"{safe}.png")
                viewer.CaptureToFile(img)
                if os.path.exists(img):
                    res.image_path = img
            except Exception:
                pass

            # 保存后复测：与 SW 同一口径，防止"实体缺料但体积报理论值"。
            v_after_save = _vol(fresh=True)
            res.volume = v_after_save or v_before_save
            if revolve_like:
                _bb = self._measure_bbox(doc, part, body)
                if _bb:
                    res.details["bbox_mm"] = [round(v, 3) for v in _bb]
                    log("包围盒实测 %.2f×%.2f×%.2f"
                        % (_bb[3] - _bb[0], _bb[4] - _bb[1], _bb[5] - _bb[2]))
                else:
                    log("⚠ 回转体包围盒测量失败（SPAWorkbench GetBoundingBox）")
            res.details["volume_before_save"] = round(v_before_save, 3)
            res.details["volume_after_save"] = round(v_after_save, 3)
            res.details["volume_source"] = "measured"
            if v_before_save > 0 and v_after_save > 0:
                _drift = abs(v_after_save - v_before_save) / v_before_save * 100.0
                if _drift > 0.01:
                    log(f"⚠ 保存前后体积不一致：{v_before_save:,.1f} → {v_after_save:,.1f}"
                        f"（{_drift:.3f}%），以保存后为准")
                    res.details["volume_rebuild_drift_pct"] = round(_drift, 4)

            res.ok = bool(res.part_path)
            if res.ok and res.volume <= 0:
                res.ok = False
                res.message = "CATIA 已保存，但实体体积测量为空/为 0"
            elif not res.ok:
                res.message = "CATIA 未能保存零件"
            else:
                res.message = "CATIA 建模完成"
        except Exception as e:
            import traceback as _tb
            log(_tb.format_exc())
            res.message = f"CATIA 建模失败：{e}"
            log(res.message)
        finally:
            try:
                if doc is not None and app.Documents.Count > 1:
                    doc.Close()
            except Exception:
                pass
            res.elapsed = time.time() - t0
            try:
                _cad_release(log)
            except Exception:
                pass
            pythoncom.CoUninitialize()
        return res
