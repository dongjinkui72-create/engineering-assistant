# P1 修复未通过验证 —— 补充材料

验证时间：2026-11-11 19:20
验证脚本：`C:\Users\董金奎\.openclaw\workspace\engimodeler\scripts\verify_p1_consistency.py`

## 结论：`_feature_consistency` 没有生效

同一张 `samples/sample_plate.png` 连跑 3 次（系统 Python，非打包环境）：

| 轮次 | 特征数 | 孔数 | conf | blocking | uncertain |
|---|---|---|---|---|---|
| 1 | 7 | 5 | **0.95** | 0 | 0 |
| 2 | 7 | 5 | **0.95** | 0 | 0 |
| 3 | **6** | 4 | 0.85 | 0 | 0 |

特征数集合 `[6, 7]` → **不稳定**，且「特征数不一致时是否报过警：**否**」。

## 而且出现了新问题：重复孔

第 1 轮和第 2 轮都是 5 个孔，但其中**两个坐标完全相同**：

```
第1轮: hole d=9 @(-65,25)   ← 重复
       hole d=9 @(-65,-25)
       hole d=9 @(-65,25)   ← 重复
       hole d=9 @(65,-25)
       hole d=9 @(65,25)

第2轮: hole d=9 @(-65,38)   ← 重复
       hole d=9 @(-65,-38)
       hole d=9 @(-65,38)   ← 重复
       hole d=9 @(65,-38)
       hole d=9 @(65,38)
```

正确结果应该是 4 个孔：`(±65, ±25)` 或 `(±65, ±38)`。

**两种错误方向都有**：
- 漏识别（第 3 轮只有 4 个孔，但槽的 y 从 25 变成 0，孔 y 也从 25 变 38）
- 重复计数（第 1、2 轮：5 个孔里有 2 个坐标相同）
- 坐标漂移（y 在 25 / 38 / 0 之间跳）

## 这三轮暴露的更根本问题

`_feature_consistency` 的设计依赖两个信号：
1. `_hole_callouts(ocr_text)` —— 从 OCR 文本抽"应该有几个孔"
2. `_image_hole_count(image_paths, spec)` —— 从图像轮廓数圆

三轮都没有触发任何警告，说明这两个信号**要么取不到值，要么判据太宽松**。

### 建议排查顺序

1. **先单独打印这两个函数的返回值**，确认它们在这张图上非空、
   且数量对得上（图上应是 4 个圆孔 + 1 个槽）。
   如果 `_hole_callouts` 返回空，整个机制就是空转。

2. **重复孔必须独立判据**，不能只靠"数量对不对"：
   ```python
   keys = [(round(f.diameter, 1), round(f.x, 1), round(f.y, 1))
           for f in spec.features if f.type == "hole"]
   if len(keys) != len(set(keys)):
       # 完全重合的孔是物理上不可能的 → 直接 uncertain + 降 confidence
   ```
   这个判据不依赖 OCR 或图像，纯结构自洽，最可靠，建议先加。

3. **坐标基准检查**：同一张图 3 次给出 y=25 / y=38，且第 3 轮槽的 y 变成 0。
   槽在板中心（y=0）还是偏置（y=25）是**几何上完全不同**的零件。
   建议：同类特征（多个 Ø9 孔）的坐标应满足某种对称性
   （本图是 ±x, ±y 四象限对称），不满足时应标记出来。

4. **`confidence` 仍然是 0.95 的问题**：
   第 1、2 轮有重复孔却报 0.95。任何"结构自洽性"扣分都应该
   直接反映到 confidence 上，而不是只记 uncertain。

## 验收标准（下次请按这个自测）

必须满足两条，否则算没修好：

1. 同一张 `sample_plate.png` 连跑 3 次，`len(spec.features)` 一致
   （要么都 6，要么都 7 但无重复孔）
2. **当特征数或孔位不一致时，至少有一轮出现 `blocking` 或
   `uncertain` 非空，或 `confidence < 0.8`**

第 2 条比第 1 条重要：识别不准可以接受，**不知道自己不准**不可接受。

## 附：请保留的既有能力（不要改坏）

- `app/vision.py` 的装配图阻断（`drawing_type == "assembly"` → blocking）
- `app/cad/catia.py::_chamfer_round` 圆盘倒角
- `app/cad/sw.py` 的射线兜底选顶面
- `app/cad/nx.py` 的 `DoUpdate` 复测与圆盘外圆边筛选