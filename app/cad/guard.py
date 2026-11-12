"""单实例闸门：同一时刻只允许一个工程软件（NX / SolidWorks / CATIA）运行。

用户要求：一次只能开一个 CAD、一个界面，禁止多开导致卡顿/崩溃。

实现两层：
  1. 跨进程文件锁：同一时刻只允许一个建模进程进入（防止两个流水线各开一家）。
  2. 进程互斥：进入前把**其它** CAD 的进程关掉（先温和关闭，超时再强制），
     绝不与另一家 CAD 并存。
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import tempfile
import threading
import time
from typing import Callable, Iterable

CAD_PROCESSES = {
    # 主进程 + 会常驻占内存的辅助进程：只杀主进程时，辅助进程会拖着工作集不退，
    # 下一次建模就会在「内存闸门」上卡住，所以按家族整体关闭。
    "SolidWorks": ("SLDWORKS.exe", "sldworks_fs.exe", "SLDWORKSCommunicator.exe"),
    # CATSysDemon 不在「必须关」的表里：它是 CATIA 的许可守护进程（约 2 MB、
    # 无窗口），在部分机器上以管理员/SYSTEM 身份运行，普通权限 taskkill 一定
    # 失败。把它算进硬性关闭会让每次 acquire 都报「无法关闭其它 CAD」直接卡死
    # 建模。它不持有任何 CAD 会话，见 BEST_EFFORT。
    "CATIA": ("CNEXT.exe", "CATIA.exe", "catstart.exe"),
    "NX": ("ugraf.exe", "run_journal.exe"),
}

# 尽力而为的辅助进程：能关就关，关不掉也不拦构建（不占 CAD 会话/界面）。
BEST_EFFORT = {
    "CATIA": ("CATSysDemon.exe",),
}

# 每家的「主界面进程」：同一时刻只允许 1 个。其它同名辅助进程不在此列。
PRIMARY_PROCESS = {
    "SolidWorks": "SLDWORKS.exe",
    "CATIA": "CNEXT.exe",
    "NX": "ugraf.exe",
}

# 已有一个主进程时怎么办：
#   True  = 保留它，适配器用 COM 复用（SW / CATIA 走 Dispatch，不会再起第二个界面）
#   False = 全关掉，适配器会用启动器新起一个（NX 走 run_journal，必然另开 ugraf，
#           留着旧的就会变成两个界面）
KEEP_ONE = {
    "SolidWorks": True,
    "CATIA": True,
    "NX": False,
}

_LOCK_PATH = os.path.join(tempfile.gettempdir(), "smartmodel_cad_single.lock")
_fh = None
# åè¿ç¨å¤çº¿ç¨ä¿æ¤：GUI è¥åæ¶ä¸åä¸¤ä¸ªå»ºæ¨¡ä»»å¡，
# åå­æä»¶éæ¯è¿ç¨çº§ç，æ¡ä¸ä½åè¿ç¨åçå¹¶åã
_thread_lock = threading.Lock()
_owner_tid = None
_depth = 0


def _log(log: Callable[[str], None], msg: str) -> None:
    try:
        log(msg)
    except Exception:
        pass


def _tasklist(names: Iterable[str]) -> dict:
    """返回 {pid: name}，只包含给定进程名。"""
    want = {n.lower() for n in names}
    out = {}
    try:
        txt = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             errors="replace").stdout
    except Exception:
        return out
    for row in csv.reader(io.StringIO(txt)):
        if len(row) < 2:
            continue
        name = (row[0] or "").strip()
        if name.lower() not in want:
            continue
        try:
            out[int((row[1] or "").strip())] = name
        except Exception:
            continue
    return out


def _alive(pid: int) -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10,
                           errors="replace").stdout
        return str(pid) in r
    except Exception:
        return False


def _ram_free_mb() -> float:
    """当前可用物理内存（MB）。只用 kernel32，避免依赖 psutil。"""
    try:
        import ctypes

        class _MEMSTATUS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(_MEMSTATUS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return float(st.ullAvailPhys) / (1024.0 * 1024.0)
    except Exception:
        return 0.0


_PRIMARY_NAMES = {str(n).lower() for n in PRIMARY_PROCESS.values()}


def _has_window(pid: int) -> bool:
    """该进程有没有可见的顶层窗口。

    没有窗口的进程（后台 helper、隐藏建模实例、卡死的 ugraf）收不到
    WM_CLOSE，温和关闭只会白等一个 grace 周期 —— 实测每次切换白等 8s，
    两家就是 20s+。判断不了时按「有窗口」处理，宁可慢也不硬杀。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hit = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) == int(pid):
                hit.append(hwnd)
                return False
            return True

        user32.EnumWindows(_cb, 0)
        return bool(hit)
    except Exception:
        return True


def _kill(pid: int, force: bool) -> None:
    """taskkill 一次性封装：force=True 走 /F /T（连同子进程）。"""
    cmd = ["taskkill"] + (["/F"] if force else []) + ["/T", "/PID", str(pid)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=15)
    except Exception:
        pass


def _close(pids: dict, log: Callable[[str], None], grace: float = 8.0,
           force_first: Iterable[str] = ()) -> None:
    """关闭进程。主界面进程先礼后兵；辅助/守护进程直接强杀。

    辅助进程（sldworks_fs、CATSysDemon 等）没有窗口，taskkill 的温和消息
    对它们无效，只会白等一个 grace 周期；而主界面要尽量让用户/软件自己收尾，
    所以只有主进程走「温和 → 超时强杀」。

    grace 只等「温和关闭」的那批。辅助进程被强杀后如果没死（服务托管/自我
    拉起），不再陪着空等 —— 旧实现会因此每次 acquire 白等 8 秒，内存闸门
    自测实测 10.08 s。现在强杀失败就立刻补一刀并报错。
    """
    if not pids:
        return
    ff = {str(x).lower() for x in (force_first or ())}
    forced: dict = {}
    polite: dict = {}
    for pid, name in pids.items():
        if str(name).lower() in ff or not _has_window(pid):
            # 辅助进程 / 没有可见窗口：温和关闭无效，直接强杀
            forced[pid] = name
            _kill(pid, force=True)
        else:
            polite[pid] = name
            _kill(pid, force=False)

    # 只等温和关闭的那批；grace=0 表示直接强制，不做等待。
    deadline = time.time() + max(0.0, grace)
    while polite and time.time() < deadline:
        polite = {p: n for p, n in polite.items() if _alive(p)}
        if not polite:
            break
        time.sleep(1.0)

    used_force = bool(forced)
    if polite:
        # 超时（或 grace=0）：温和关闭升级为强杀
        used_force = True
        for pid in list(polite):
            _kill(pid, force=True)
        time.sleep(1.0)
    # 强杀过但还活着的（守护进程会自我拉起）：再补一刀，仍活则下面报错
    for pid in [p for p, n in forced.items() if _alive(p)]:
        _kill(pid, force=True)

    still = [f"{n}({p})" for p, n in pids.items() if _alive(p)]
    if still:
        raise RuntimeError("无法关闭其它 CAD 进程：" + "、".join(still))
    _log(log, ("已强制关闭：" if used_force else "已关闭：")
         + "、".join(f"{n}({p})" for p, n in pids.items()))


def _sweep_others(cad: str, log: Callable[[str], None] = lambda m: None,
                  rounds: int = 2, grace: float = 8.0) -> int:
    """关掉**其它** CAD（整族，含辅助进程），并复核没有被自动拉起。

    某些 CAD 的辅助进程会在主进程退出后自我重启（索引/通信进程），
    所以要复核一轮；复核后仍有残留就报错，绝不带着第二家 CAD 继续建。
    """
    total = 0
    for _ in range(max(1, int(rounds))):
        found = False
        for other, names in CAD_PROCESSES.items():
            if other == cad:
                continue
            pids = _tasklist(names)
            if not pids:
                continue
            found = True
            _log(log, f"检测到 {other} 仍在运行，先关闭：" +
                 "、".join(f"{n}({p})" for p, n in pids.items()))
            _helpers = {str(n).lower() for n in names}
            _helpers -= _PRIMARY_NAMES
            _close(pids, log, grace=grace, force_first=_helpers)
            total += len(pids)
        if not found:
            return total
    # 尽力而为：许可守护进程关不掉时只记一行，不当成「第二家 CAD 在跑」
    for other, names in BEST_EFFORT.items():
        if other == cad:
            continue
        pids = _tasklist(names)
        if not pids:
            continue
        for pid in pids:
            _kill(pid, force=True)
        rest = _tasklist(names)
        if rest:
            _log(log, "%s 许可守护进程仍在运行（%s，无窗口、不占 CAD 会话，忽略）"
                 % (other, "、".join("%s(%s)" % (n, p) for p, n in rest.items())))
        else:
            total += len(pids)
    left = []
    for other, names in CAD_PROCESSES.items():
        if other == cad:
            continue
        pids = _tasklist(names)
        if pids:
            left += [f"{n}({p})" for p, n in pids.items()]
    if left:
        raise RuntimeError("仍有其它 CAD 进程无法关闭：" + "、".join(left))
    return total


def free_others(cad: str, log: Callable[[str], None] = lambda m: None,
                fast: bool = False) -> int:
    """把**其它** CAD 关掉，并把内存还回来（不等锁、不建锁）。

    用途：内存闸门（memguard）在 acquire 之前就会早退，
    此时 SolidWorks 可能还占着 500+ MB 内存，导致 CATIA 永远等不到
    1200 MB 可用 —— 用户是要求「同一时刻只开一个 CAD」，
    所以闸门早期就该按这条规则先关别人，再判内存。
    fast=True 用于「内存不足、本次根本不建模」的早退分支：不等温和关闭，
    直接强制释放内存（用户要求同一时刻只开一个 CAD）。
    返回关掉的进程数。
    """
    if os.environ.get("EA_CAD_GUARD", "1").strip() in ("0", "false", "False", "off"):
        return 0
    try:
        closed = _sweep_others(cad, log, grace=(0.0 if fast else 8.0))
    except Exception as e:
        # 内存闸门这一层不抛：关不掉时下面的可用内存判定会给出结论，
        # 但必须把原因写进日志，别让「还有一家 CAD 在跑」变成静默事实。
        _log(log, f"关闭其它 CAD 失败：{e}")
        closed = 0
    if closed:
        time.sleep(0.5 if fast else 2.0)   # 让 Windows 真正回收工作集后再判可用内存
    return closed


def _acquire_impl(cad: str, log: Callable[[str], None] = lambda m: None,
                  timeout: float = 1800.0) -> None:
    """获取全局独占权，并确保其它 CAD 已退出。"""
    global _fh
    if os.environ.get("EA_CAD_GUARD", "1").strip() in ("0", "false", "False", "off"):
        return
    if _fh is not None:
        # 同一进程重入：已经独占，直接复用。否则第二次 open + locking 会自锁，
        # 一直等到 timeout（表现为「另一个建模任务正在占用 CAD」的假死）。
        return
    try:
        import msvcrt
    except Exception:
        return

    deadline = time.time() + max(timeout, 1.0)
    fh = open(_LOCK_PATH, "a+", encoding="utf-8")
    while True:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            break
        except OSError:
            if time.time() > deadline:
                fh.close()
                raise RuntimeError("另一个建模任务正在占用 CAD（同一时刻只允许一个），请等待其结束")
            time.sleep(2.0)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {cad} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.flush()
    except Exception:
        pass
    _fh = fh

    _log(log, f"单实例闸门：本次使用 {cad}")
    _ram0 = _ram_free_mb()
    _closed = _sweep_others(cad, log)
    if _closed:
        time.sleep(1.5)   # 等工作集真正归还，再报「让出多少内存」
        _log(log, "已关闭其它 CAD %d 个进程，可用内存 %.0f → %.0f MB"
             % (_closed, _ram0, _ram_free_mb()))
    else:
        _log(log, "确认另外两家 CAD 均未运行（可用内存 %.0f MB）" % _ram0)

    # 同一家也不允许出现多个主进程：同一时刻只能有一个界面。
    # 只对「主程序」计数，避免误杀辅助进程（CATIA 的 CATStart）。
    primary = PRIMARY_PROCESS.get(cad)
    if primary:
        own = _tasklist((primary,))
        if own and not KEEP_ONE.get(cad, True):
            # NX：run_journal 会另起一个 ugraf，必须先把现存的全关掉；
            # 顺带清掉上一次超时残留的 run_journal。
            pids = _tasklist(CAD_PROCESSES.get(cad, ()))
            _log(log, f"{cad} 本次独占启动，先关闭已运行的进程：" +
                 "、".join(f"{n}({p})" for p, n in pids.items()))
            _close(pids, log)
        elif len(own) > 1:
            keep = min(own)
            extra = {p: n for p, n in own.items() if p != keep}
            _log(log, f"检测到 {cad} 多开（{len(own)} 个主进程），关闭多余进程：" +
                 "、".join(f"{n}({p})" for p, n in extra.items()))
            _close(extra, log)
            # 关掉多余实例后确认只剩一个，否则适配器可能连到已被关闭的进程。
            left = _tasklist((primary,))
            if len(left) != 1:
                raise RuntimeError(
                    f"{cad} 单实例检查失败：期望 1 个 {primary}，实际 {len(left)} 个")


def acquire(cad: str, log: Callable[[str], None] = lambda m: None,
            timeout: float = 1800.0) -> None:
    """è·åç¬å æã

    çº¿ç¨çº§：åä¸è¿ç¨åå¤ä¸ªå»ºæ¨¡çº¿ç¨ä¸²è¡å（GUI è¿ç¹ä¸¤æ¬¡ä¹ä¼æé）ã
    çº¿ç¨åéå¥：åä¸çº¿ç¨åµå¥è°ç¨ç´æ¥å¤ç¨，ä¸éå¤å¼/å³ CADã
    """
    global _owner_tid, _depth
    if os.environ.get("EA_CAD_GUARD", "1").strip() in ("0", "false", "False", "off"):
        return
    tid = threading.get_ident()
    if _owner_tid == tid and _fh is not None:
        _depth += 1
        return
    wait = max(float(timeout), 1.0)
    if not _thread_lock.acquire(timeout=wait):
        raise RuntimeError("å¦ä¸ä¸ªå»ºæ¨¡çº¿ç¨æ­£å¨å ç¨ CAD（åä¸æ¶å»åªåè®¸ä¸ä¸ª），è¯·ç­å¾å¶ç»æ")
    try:
        _acquire_impl(cad, log, timeout)
    except BaseException:
        try:
            _thread_lock.release()
        except Exception:
            pass
        raise
    _owner_tid = tid
    _depth = 1


def release(log: Callable[[str], None] = lambda m: None) -> None:
    global _fh, _owner_tid, _depth
    if _owner_tid is not None and _owner_tid != threading.get_ident():
        # éæéçº¿ç¨è°ç¨：å¿½ç¥，é¿åè¯¯æ¾å«äººçé
        return
    if _owner_tid is not None:
        _depth -= 1
        if _depth > 0:
            return
    _owner_tid = None
    _depth = 0
    if _fh is None:
        try:
            _thread_lock.release()
        except Exception:
            pass
        return
    try:
        import msvcrt
        _fh.seek(0)
        msvcrt.locking(_fh.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        _fh.close()
    except Exception:
        pass
    _fh = None
    try:
        _thread_lock.release()
    except Exception:
        pass
