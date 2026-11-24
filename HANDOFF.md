# 断点记录（明天从这里继续）

更新时间：2026-11-06 23:5x

## 已完成并验证
1. 环境：NX 12.0.2（D:\ug\NX 12.0）、SolidWorks 2024（D:\soild\...）、CATIA V5R21（E:\catia\anzhuang）
2. NX 适配器 app/cad/nx.py：后台 run_journal.exe 跑日志，方块/圆柱+布尔切除，保存 .prt，体积测量（误差 0.08%）
3. SolidWorks 适配器 app/cad/sw.py：草图+拉伸，顶面基准面+切除，保存 SLDPRT/STEP/BMP（误差 0.00%）
4. CATIA 适配器 app/cad/catia.py：草图+凸台，偏置平面+凹槽，保存 CATPart/STEP（误差 0.00%）
   - 注意：CATIA 需要 doc.Activate()+预热；文件名不能含中文（自动转 ASCII）
5. 几何内核 app/geom.py、体积/面积计算 app/calc.py
6. CAM 模块 app/cam/gcode.py：轮廓/型腔/钻孔，生成 .nc + .svg 预览 + 加工说明（自检通过）

## 明天待办
1. app/vision.py 实测（qwen3-vl-plus，用自绘的三视图测试）
2. 生成 3 张带尺寸的零件图纸（tools/make_drawings.py）
3. 总流水线 app/pipeline.py + 界面 app/ui/main_window.py（PySide6）+ main.py
4. 图标（可用 qwen-image 接口生成）→ .ico
5. PyInstaller 打包 exe
6. 三张图纸 × 三款软件 端到端实测，输出报告
7. 打包 zip 交付

## 已知注意点
- NX STEP 导出：StepCreator.ExportFromOption 只读，暂未导出 STEP（PRT 正常）
- SW 中文文件名正常，CATIA 需转 ASCII
- 测试脚本：tools/t_nx.py、t_sw.py、t_catia.py、t_cam.py

## 2026-11-09 单实例闸门（用户硬约束：一次只能开一个 CAD、一个界面）
- app/cad/guard.py
  - 跨进程文件锁（msvcrt，锁 tempdir\smartmodel_cad_single.lock）
  - 线程级串行化：同进程多线程串行，线程内重入只增深度，不死锁
  - 换 CAD 先关旧 CAD：先温和 taskkill，20s 后 /F /T
  - 同家多开只留最小 PID（SW/CATIA 复用 COM）；NX 启动前清空所有 ugraf/run_journal
- 修复真实卡死：CATIA `subprocess.Popen` 继承调用方 stdout 管道 -> 管道永不 EOF，
  调用方（含 `| Select-Object -Last`）会一直挂到 CATIA 退出。实测 CATIA 建模 13:52:30 就写盘完成，
  但外层挂了 10 分钟以上。改为 DEVNULL + close_fds 后，同一零件 14s 返回。
  NX 同样改为写文件 nx_run.log，不用 PIPE。
- 测试
  - tools\guard_double.py   跨进程/多开/换家 PASS
  - tools\guard_thread.py   线程串行 + 重入 + 释放 PASS
  - tools\guard_busy_probe.py  另一进程持锁时 REFUSED after 6.0s
- 实测切换：SW 建模后 SLDWORKS.exe 存在；转 CATIA 时 SW 被关闭，只剩 1 个 CNEXT + 1 个 CATSTART
- 备份：app\cad\guard.py.bak_thread_*、catia.py.bak_popen_*、nx.py.bak_nxlog_*
